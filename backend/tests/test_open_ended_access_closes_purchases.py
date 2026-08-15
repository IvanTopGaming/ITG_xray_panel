"""Somebody with permanent access cannot be sold a subscription that would take it away.

Every tariff in this deployment routes through the same inbounds, so a user has exactly one key per
node and any purchase rewrites that key's date and its traffic limit. A holder of an open-ended
unlimited grant who bought a 30-day 300 GB tariff would pay money to be worse off -- silently, with no
error anywhere and nothing in the panel showing what happened.

The accepted cost is that an upgrade is closed for such a user too. While tariffs share inbounds
there is no way to express one without the other; the admin edits the grant instead.

These build the bot-api role's own app: the question is what a role with no local Xray offers a
specific user, and the surfaces under test (`/bot-service/*`, `/billing/checkout`) exist on no other
role.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Payment, SystemSetting, Tariff, TariffItem, TelegramUser, UserTariffAccess
from panel_core.xray import gateway as gw

from tests.schema import ensure_schema


BOT_TOKEN = "grant-rework-bot-token"


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
        paid = Tariff(name="Standard", price_rub=150, period_days=30, visibility="public", enabled=True)
        trial = Tariff(name="Trial", price_rub=0, period_days=1, visibility="public", enabled=True, is_trial=True)
        db.session.add_all([paid, trial])
        db.session.flush()
        db.session.add(TariffItem(tariff_id=paid.id, inbound_tag="hiks", traffic_gb=300, panel_id=2))
        db.session.add(TariffItem(tariff_id=trial.id, inbound_tag="hiks", traffic_gb=10, panel_id=2))
        db.session.add_all([TelegramUser(telegram_id=7, language="ru"), TelegramUser(telegram_id=8, language="ru")])
        db.session.commit()
        app.config["PAID_TARIFF_ID"] = paid.id

    yield app
    _reset_scheduler()


@pytest.fixture
def client(botapi_app):
    return botapi_app.test_client()


def _auth():
    return {"Authorization": f"Bearer {BOT_TOKEN}"}


@pytest.fixture
def open_ended_holder(botapi_app):
    with botapi_app.app_context():
        granted = Tariff(name="Premium", price_rub=0, period_days=30, visibility="private", enabled=True)
        db.session.add(granted)
        db.session.flush()
        db.session.add(TariffItem(tariff_id=granted.id, inbound_tag="hiks", traffic_gb=0, panel_id=2))
        db.session.add(UserTariffAccess(telegram_id=7, tariff_id=granted.id, billing="free", access_until=None))
        db.session.commit()


@pytest.fixture
def dated_holder(botapi_app):
    with botapi_app.app_context():
        granted = Tariff(name="Premium", price_rub=0, period_days=30, visibility="private", enabled=True)
        db.session.add(granted)
        db.session.flush()
        db.session.add(TariffItem(tariff_id=granted.id, inbound_tag="hiks", traffic_gb=0, panel_id=2))
        db.session.add(
            UserTariffAccess(
                telegram_id=8,
                tariff_id=granted.id,
                billing="free",
                access_until=datetime.utcnow() + timedelta(days=30),
            )
        )
        db.session.commit()


def test_the_catalogue_is_empty_for_an_open_ended_holder(client, open_ended_holder):
    resp = client.get("/api/bot-service/tariffs?for=7", headers=_auth())

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json() == [], (
        "a purchase would rewrite the one key this user has on each node and replace access they hold "
        f"forever with thirty days; got {resp.get_json()!r}"
    )


def test_checkout_is_refused_for_an_open_ended_holder(botapi_app, client, open_ended_holder):
    with patch("panel_core.services.billing.yookassa.Payment.create") as mock_create:
        resp = client.post(
            "/api/billing/checkout",
            json={"telegram_id": 7, "tariff_id": botapi_app.config["PAID_TARIFF_ID"], "lang": "ru"},
            headers=_auth(),
        )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert "open_ended_access" in resp.get_data(as_text=True), (
        f"the refusal must name its reason so the bot can say why rather than 'payment failed'; got {resp.data!r}"
    )
    assert not mock_create.called, "no invoice may be created for a purchase that cannot improve anything"
    with botapi_app.app_context():
        assert Payment.query.count() == 0, "a refused checkout must leave no pending payment behind"


def test_the_trial_is_unavailable_to_an_open_ended_holder(client, open_ended_holder):
    resp = client.get("/api/bot-service/users/7/state", headers=_auth())

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["trial_available"] is False, (
        "the trial provisions a one-day tariff onto the same inbounds and would replace permanent access with it"
    )


def test_claiming_the_trial_anyway_is_refused_without_burning_it(botapi_app, client, open_ended_holder):
    resp = client.post("/api/bot-service/trial/activate", json={"telegram_id": 7}, headers=_auth())

    assert resp.status_code == 409, resp.get_data(as_text=True)
    with botapi_app.app_context():
        user = db.session.get(TelegramUser, 7)
        assert user.trial_used_at is None, (
            "a refused trial must stay unclaimed -- burning the single attempt on a request the panel "
            f"declined would cost the user something for nothing; got {user.trial_used_at!r}"
        )


def test_a_dated_grant_does_not_close_purchases(client, dated_holder):
    resp = client.get("/api/bot-service/tariffs?for=8", headers=_auth())

    assert resp.get_json() != [], (
        "only open-ended access closes the catalogue -- a grant that ends is exactly the case where "
        "buying more time is the right thing to offer"
    )


def test_a_user_with_no_grant_still_sees_the_catalogue(client):
    resp = client.get("/api/bot-service/tariffs?for=999", headers=_auth())

    assert resp.get_json() != [], "the negative control: an empty catalogue for everyone would pass every test above"


def _mock_yk_payment():
    return SimpleNamespace(id="yk-open-ended-1", confirmation=SimpleNamespace(confirmation_url="https://yk.test/pay"))


def test_a_user_with_no_grant_can_still_check_out(botapi_app, client):
    with patch("panel_core.services.billing.yookassa.Payment.create", return_value=_mock_yk_payment()):
        resp = client.post(
            "/api/billing/checkout",
            json={"telegram_id": 999, "tariff_id": botapi_app.config["PAID_TARIFF_ID"], "lang": "ru"},
            headers=_auth(),
        )

    assert resp.status_code == 200, (
        f"the negative control for the checkout refusal; got {resp.status_code} {resp.get_data(as_text=True)}"
    )
