"""§63: a tariff that cannot be delivered must not reach an invoice.

A `TariffItem` says "issue a key on this inbound"; its `panel_id` says "on that node". Since phase 3b
the master refuses to save an item without one, but installations from the monolith era still hold
such rows -- back then there were no nodes and everything was issued locally.

bot-api runs no Xray of its own, so an item with no `panel_id` points at nothing. Until this wave the
tariff passed the catalogue, passed `_ensure_tariff_available`, got an invoice, and only after the
user had paid did `apply_tariff_for_user` raise `LocalXrayUnavailable` -- money taken, nothing issued.
Wave 0 made that failure loud instead of silent; it did not move it in front of the money.

These tests pin the order: the catalogue does not show it, the checkout refuses it with no `Payment`
row written, and the trial neither offers it nor burns the user's single attempt on it. They build the
bot-api role's own app, because the whole question is what a role with no local Xray can deliver --
under the plain `app` fixture, whose gateway is a `LocalXrayGateway`, every one of them is deliverable
and every assertion here would pass vacuously.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Payment, SystemSetting, Tariff, TariffItem, TelegramUser
from panel_core.xray import gateway as gw

from tests.schema import ensure_schema


BOT_TOKEN = "wave5a-bot-token"


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    try:
        scheduler.remove_all_jobs()
    except Exception:
        pass


@pytest.fixture
def botapi_app(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "bot")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/botapi.db"))
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.com")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    import importlib

    app = importlib.import_module("panel_core.roles.botapi").create_app()

    with app.app_context():
        db.session.add_all(
            [
                SystemSetting(key="bot_service_token", value=BOT_TOKEN),
                SystemSetting(key="yookassa_shop_id", value="test-shop"),
                SystemSetting(key="yookassa_secret_key", value="test-secret"),
                SystemSetting(key="yookassa_return_url", value="https://t.me/itg_bot"),
            ]
        )
        db.session.commit()

    yield app

    _reset_scheduler()


@pytest.fixture
def client(botapi_app):
    return botapi_app.test_client()


def _auth():
    return {"Authorization": f"Bearer {BOT_TOKEN}"}


def _make_tariff(app, *, name, panel_ids, is_trial=False, enabled=True, price=150):

    with app.app_context():
        tariff = Tariff(
            name=name,
            price_rub=price,
            period_days=30,
            visibility="public",
            enabled=enabled,
            is_trial=is_trial,
        )
        tariff.items = [
            TariffItem(inbound_tag=f"vless-{idx}", traffic_gb=0, panel_id=pid, sort_order=idx)
            for idx, pid in enumerate(panel_ids)
        ]
        db.session.add(tariff)
        db.session.commit()
        return tariff.id


def _mock_yk_payment():
    return SimpleNamespace(id="yk-wave5a-1", confirmation=SimpleNamespace(confirmation_url="https://yk.test/pay"))


def _checkout(client, tariff_id, telegram_id=4242):
    return client.post(
        "/api/billing/checkout",
        json={"telegram_id": telegram_id, "tariff_id": tariff_id, "lang": "ru"},
        headers=_auth(),
    )


# --- the checkout ------------------------------------------------------------------------------


def test_a_tariff_whose_items_all_lack_a_panel_id_never_reaches_an_invoice(botapi_app, client):
    tariff_id = _make_tariff(botapi_app, name="Legacy 30d", panel_ids=[None, None])

    with patch("panel_core.services.billing.yookassa.Payment.create") as mock_create:
        resp = _checkout(client, tariff_id)

    assert resp.status_code == 400, (
        f"checkout accepted a tariff with no deliverable item (HTTP {resp.status_code}). "
        "The user would have paid for a grant this role cannot perform."
    )
    assert resp.get_json()["error"] == "tariff_not_available"
    assert mock_create.call_count == 0, "YooKassa was asked to create an invoice for an undeliverable tariff"

    with botapi_app.app_context():
        assert Payment.query.count() == 0, (
            "a Payment row was written for an undeliverable tariff. The refusal has to land before the "
            "row exists, or poll_pending_payments and cleanup_old_payments inherit a payment nothing can fulfil."
        )


def test_a_mixed_tariff_is_refused_because_of_its_one_bad_item(botapi_app, client):
    """Two items on nodes, one with no panel_id. The tariff is not two-thirds deliverable -- it is refused.

    `apply_tariff_for_user` checks its local items before it touches any node, so today such a purchase
    provisions nothing at all; but nothing about that ordering is guaranteed by the contract, and a check
    written against the tariff rather than against every item would let this one through.
    """

    tariff_id = _make_tariff(botapi_app, name="Mixed 30d", panel_ids=[7, 9, None])

    with patch("panel_core.services.billing.yookassa.Payment.create") as mock_create:
        resp = _checkout(client, tariff_id)

    assert resp.status_code == 400, (
        f"checkout accepted a tariff whose third item has no panel_id (HTTP {resp.status_code}). "
        "The check must look at every item, not at the tariff as a whole."
    )
    assert resp.get_json()["error"] == "tariff_not_available"
    mock_create.assert_not_called()
    with botapi_app.app_context():
        assert Payment.query.count() == 0


def test_a_tariff_with_no_items_at_all_never_reaches_an_invoice(botapi_app, client):
    """`apply_payment` already refuses this one -- after the money. `create_checkout` never did."""

    tariff_id = _make_tariff(botapi_app, name="Empty", panel_ids=[])

    with patch("panel_core.services.billing.yookassa.Payment.create") as mock_create:
        resp = _checkout(client, tariff_id)

    assert resp.status_code == 400, f"checkout invoiced a tariff carrying no items (HTTP {resp.status_code})"
    mock_create.assert_not_called()
    with botapi_app.app_context():
        assert Payment.query.count() == 0


def test_a_fully_routed_tariff_still_checks_out(botapi_app, client):
    """The guard must refuse the broken tariff and nothing else."""

    tariff_id = _make_tariff(botapi_app, name="Standard 30d", panel_ids=[7, 9])

    with patch("panel_core.services.billing.yookassa.Payment.create") as mock_create:
        mock_create.return_value = _mock_yk_payment()
        resp = _checkout(client, tariff_id)

    assert resp.status_code == 200, (
        f"checkout refused a tariff whose every item names a node (HTTP {resp.status_code}: "
        f"{resp.get_json()}). The guard is too wide."
    )
    assert resp.get_json()["confirmation_url"] == "https://yk.test/pay"
    with botapi_app.app_context():
        assert Payment.query.count() == 1


# --- the catalogue -----------------------------------------------------------------------------


def test_the_catalogue_hides_what_cannot_be_delivered_and_keeps_what_can(botapi_app, client):
    good = _make_tariff(botapi_app, name="Standard 30d", panel_ids=[7])
    legacy = _make_tariff(botapi_app, name="Legacy 30d", panel_ids=[None])
    mixed = _make_tariff(botapi_app, name="Mixed 30d", panel_ids=[7, None])
    empty = _make_tariff(botapi_app, name="Empty", panel_ids=[])

    resp = client.get("/api/bot-service/tariffs?for=4242", headers=_auth())
    assert resp.status_code == 200
    listed = {t["id"] for t in resp.get_json()}

    assert good in listed, "a deliverable tariff vanished from the catalogue"
    assert legacy not in listed, (
        "the catalogue offered a tariff with no panel_id on any item. The user would press buy on "
        "something that cannot be issued."
    )
    assert mixed not in listed, "the catalogue offered a tariff with one item lacking a panel_id"
    assert empty not in listed, "the catalogue offered a tariff carrying no items"


# --- the trial ---------------------------------------------------------------------------------


def test_an_undeliverable_trial_is_neither_offered_nor_burned(botapi_app, client):
    _make_tariff(botapi_app, name="Trial", panel_ids=[None], is_trial=True, price=0)
    with botapi_app.app_context():
        db.session.add(TelegramUser(telegram_id=4242, language="ru"))
        db.session.commit()

    state = client.get("/api/bot-service/users/4242/state", headers=_auth())
    assert state.status_code == 200
    assert state.get_json()["trial_available"] is False, (
        "the bot was told a trial is available while the only trial tariff cannot be issued. "
        "The user presses the button and gets an error."
    )

    resp = client.post("/api/bot-service/trial/activate", json={"telegram_id": 4242}, headers=_auth())
    assert resp.status_code == 404, f"the trial was activated on an undeliverable tariff (HTTP {resp.status_code})"

    with botapi_app.app_context():
        user = db.session.get(TelegramUser, 4242)
        assert user.trial_used_at is None, (
            "the refusal burned the user's single trial attempt. The claim must not be taken for a "
            "tariff that was never going to be issued."
        )


def test_a_deliverable_trial_still_activates(botapi_app, client):
    _make_tariff(botapi_app, name="Trial", panel_ids=[7], is_trial=True, price=0)
    with botapi_app.app_context():
        db.session.add(TelegramUser(telegram_id=4242, language="ru"))
        db.session.commit()

    state = client.get("/api/bot-service/users/4242/state", headers=_auth())
    assert state.get_json()["trial_available"] is True

    with patch("panel_core.api.bot_service.apply_tariff_for_user") as mock_apply:
        mock_apply.return_value = {"expires_at_ms": 1_900_000_000_000, "clients": []}
        resp = client.post("/api/bot-service/trial/activate", json={"telegram_id": 4242}, headers=_auth())

    assert resp.status_code == 200, f"a deliverable trial was refused (HTTP {resp.status_code}: {resp.get_json()})"
    mock_apply.assert_called_once()
    with botapi_app.app_context():
        assert db.session.get(TelegramUser, 4242).trial_used_at is not None


def test_a_failing_provision_still_gives_the_trial_back(botapi_app, client):
    """The rollback that exists today must survive this wave: a failed attempt is not a used trial."""

    _make_tariff(botapi_app, name="Trial", panel_ids=[7], is_trial=True, price=0)
    with botapi_app.app_context():
        db.session.add(TelegramUser(telegram_id=4242, language="ru"))
        db.session.commit()

    with patch("panel_core.api.bot_service.apply_tariff_for_user") as mock_apply:
        mock_apply.side_effect = RuntimeError("node unreachable")
        resp = client.post("/api/bot-service/trial/activate", json={"telegram_id": 4242}, headers=_auth())

    assert resp.status_code == 500, "a provisioning failure must not be reported to the bot as success"

    with botapi_app.app_context():
        assert db.session.get(TelegramUser, 4242).trial_used_at is None, (
            "a failed provisioning consumed the user's only trial"
        )


# --- the payment that was already taken ---------------------------------------------------------


def test_a_payment_taken_before_this_wave_fails_without_reaching_provisioning(botapi_app):
    """The upgrade inherits pending payments for tariffs it now refuses; they must close, not crash.

    Before this wave `apply_payment` reached `apply_tariff_for_user`, caught `LocalXrayUnavailable` and
    marked the payment failed. It still must end failed -- with the user told -- but now the tariff is
    rejected during revalidation, before provisioning is attempted at all.
    """

    tariff_id = _make_tariff(botapi_app, name="Legacy 30d", panel_ids=[None])

    with botapi_app.app_context():
        from panel_core.services import billing

        payment = Payment(
            yookassa_id="yk-inherited-1",
            telegram_id=4242,
            tariff_id=tariff_id,
            tariff_snapshot={},
            amount_rub=150,
            status="pending",
            metadata_json={"lang": "ru"},
        )
        db.session.add(payment)
        db.session.commit()
        payment_id = payment.id

        with (
            patch("panel_core.services.billing.provisioning.apply_tariff_for_user") as mock_apply,
            patch("panel_core.services.billing.bot_events.publish") as mock_publish,
        ):
            billing.apply_payment(payment)

        assert mock_apply.call_count == 0, (
            "apply_payment still went into provisioning for a tariff it can no longer deliver"
        )
        assert db.session.get(Payment, payment_id).status == "failed"
        published = [call.args[0] for call in mock_publish.call_args_list]
        assert "payment_failed" in published, (
            "the payment failed without telling the user. `payment_failed` is the only way they find out."
        )
