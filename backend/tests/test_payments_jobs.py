import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from panel_core.extensions import db
from panel_core.models import Payment, SystemSetting, Tariff, TariffItem


@pytest.fixture
def configured(app):
    with app.app_context():
        db.session.add_all(
            [
                SystemSetting(key="yookassa_shop_id", value="test-shop"),
                SystemSetting(key="yookassa_secret_key", value="test_secret"),
            ]
        )
        db.session.commit()


@pytest.fixture
def tariff(app, configured):
    with app.app_context():
        t = Tariff(
            name="Standard",
            price_rub=150,
            period_days=30,
            visibility="public",
            enabled=True,
            is_trial=False,
        )
        t.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        db.session.add(t)
        db.session.commit()
        yield t.id


def _insert_pending(app, tariff_id, age_seconds=600, yk_id="yk-poll-1"):
    with app.app_context():
        p = Payment(
            yookassa_id=yk_id,
            telegram_id=42,
            tariff_id=tariff_id,
            tariff_snapshot={"name": "x", "price_rub": 150, "period_days": 30, "items": []},
            amount_rub=150,
            status="pending",
            metadata_json={"lang": "ru"},
        )
        db.session.add(p)
        db.session.flush()
        p.created_at = dt.datetime.utcnow() - dt.timedelta(seconds=age_seconds)
        db.session.commit()
        return p.id


def test_poll_promotes_succeeded_payments(app, tariff):
    pid = _insert_pending(app, tariff)
    from panel_core.jobs.payments import poll_pending_payments

    with (
        app.app_context(),
        patch("panel_core.jobs.payments.yookassa.Payment.find_one") as mock_find,
        patch("panel_core.services.billing.provisioning.apply_tariff_for_user") as mock_provision,
        patch("panel_core.services.billing.bot_events.publish"),
    ):
        mock_find.return_value = SimpleNamespace(status="succeeded")
        mock_provision.return_value = {"clients": [], "expires_at_ms": 9999999999000, "source": "yookassa"}
        poll_pending_payments()
    with app.app_context():
        assert db.session.get(Payment, pid).status == "succeeded"


def test_poll_marks_cancelled(app, tariff):
    pid = _insert_pending(app, tariff)
    from panel_core.jobs.payments import poll_pending_payments

    with (
        app.app_context(),
        patch("panel_core.jobs.payments.yookassa.Payment.find_one") as mock_find,
        patch("panel_core.services.billing.bot_events.publish") as mock_publish,
        patch("panel_core.jobs.payments.bot_events.publish") as mock_publish_local,
    ):
        mock_find.return_value = SimpleNamespace(status="canceled")
        poll_pending_payments()
    with app.app_context():
        assert db.session.get(Payment, pid).status == "cancelled"

    assert (mock_publish_local.call_args or mock_publish.call_args).args[0] == "payment_cancelled"


def test_poll_cancel_event_carries_chat_coords(app, tariff):

    pid = _insert_pending(app, tariff)
    with app.app_context():
        p = db.session.get(Payment, pid)
        p.chat_id = 42_000
        p.message_id = 555
        db.session.commit()

    from panel_core.jobs.payments import poll_pending_payments

    with (
        app.app_context(),
        patch("panel_core.jobs.payments.yookassa.Payment.find_one") as mock_find,
        patch("panel_core.jobs.payments.bot_events.publish") as mock_publish,
    ):
        mock_find.return_value = SimpleNamespace(status="canceled")
        poll_pending_payments()

    call = mock_publish.call_args
    assert call.args[0] == "payment_cancelled"
    payload = call.args[2] if len(call.args) >= 3 else call.kwargs.get("payload") or call.args[-1]
    assert payload["chat_id"] == 42_000
    assert payload["message_id"] == 555


def test_poll_skips_payments_younger_than_30s(app, tariff):
    pid = _insert_pending(app, tariff, age_seconds=10)
    from panel_core.jobs.payments import poll_pending_payments

    with app.app_context(), patch("panel_core.jobs.payments.yookassa.Payment.find_one") as mock_find:
        mock_find.return_value = SimpleNamespace(status="succeeded")
        poll_pending_payments()
    mock_find.assert_not_called()
    with app.app_context():
        assert db.session.get(Payment, pid).status == "pending"


def test_poll_skips_payments_older_than_24h(app, tariff):
    _insert_pending(app, tariff, age_seconds=25 * 3600)
    from panel_core.jobs.payments import poll_pending_payments

    with app.app_context(), patch("panel_core.jobs.payments.yookassa.Payment.find_one") as mock_find:
        mock_find.return_value = SimpleNamespace(status="succeeded")
        poll_pending_payments()
    mock_find.assert_not_called()


def test_poll_swallows_individual_failures(app, tariff):
    pid_a = _insert_pending(app, tariff, yk_id="yk-a")
    pid_b = _insert_pending(app, tariff, yk_id="yk-b")
    from panel_core.jobs.payments import poll_pending_payments

    def find_side_effect(yk_id):
        if yk_id == "yk-a":
            raise RuntimeError("transient")
        return SimpleNamespace(status="succeeded")

    with (
        app.app_context(),
        patch("panel_core.jobs.payments.yookassa.Payment.find_one", side_effect=find_side_effect),
        patch("panel_core.services.billing.provisioning.apply_tariff_for_user") as mock_provision,
        patch("panel_core.services.billing.bot_events.publish"),
    ):
        mock_provision.return_value = {"clients": [], "expires_at_ms": 9999999999000, "source": "yookassa"}
        poll_pending_payments()
    with app.app_context():
        assert db.session.get(Payment, pid_a).status == "pending"
        assert db.session.get(Payment, pid_b).status == "succeeded"


def _insert_succeeded(app, tariff_id, age_days=1, yk_id="yk-succ-1"):
    with app.app_context():
        p = Payment(
            yookassa_id=yk_id,
            telegram_id=42,
            tariff_id=tariff_id,
            tariff_snapshot={"name": "x", "price_rub": 150, "period_days": 30, "items": []},
            amount_rub=150,
            status="succeeded",
            metadata_json={"lang": "ru"},
        )
        db.session.add(p)
        db.session.flush()
        p.created_at = dt.datetime.utcnow() - dt.timedelta(days=age_days)
        db.session.commit()
        return p.id


def test_reconcile_revokes_refunded(app, tariff):
    pid = _insert_succeeded(app, tariff)
    from panel_core.jobs.payments import reconcile_refunds

    with (
        app.app_context(),
        patch("panel_core.services.billing.yookassa.Payment.find_one") as mock_find,
        patch("panel_core.services.billing.bot_events.publish") as mock_publish,
    ):
        mock_find.return_value = SimpleNamespace(refunded_amount=SimpleNamespace(value="150.00"))
        reconcile_refunds()
    with app.app_context():
        assert db.session.get(Payment, pid).status == "refunded"
    assert mock_publish.call_args.args[0] == "payment_refunded"


def test_reconcile_ignores_unrefunded(app, tariff):
    pid = _insert_succeeded(app, tariff)
    from panel_core.jobs.payments import reconcile_refunds

    with (
        app.app_context(),
        patch("panel_core.services.billing.yookassa.Payment.find_one") as mock_find,
    ):
        mock_find.return_value = SimpleNamespace(refunded_amount=SimpleNamespace(value="0.00"))
        reconcile_refunds()
    with app.app_context():
        assert db.session.get(Payment, pid).status == "succeeded"


def test_reconcile_skips_old_succeeded(app, tariff):
    pid = _insert_succeeded(app, tariff, age_days=31)
    from panel_core.jobs.payments import reconcile_refunds

    with (
        app.app_context(),
        patch("panel_core.services.billing.yookassa.Payment.find_one") as mock_find,
    ):
        mock_find.return_value = SimpleNamespace(refunded_amount=SimpleNamespace(value="150.00"))
        reconcile_refunds()
    mock_find.assert_not_called()
    with app.app_context():
        assert db.session.get(Payment, pid).status == "succeeded"


def test_cleanup_cancels_stale_pending(app, tariff):
    pid = _insert_pending(app, tariff, age_seconds=25 * 3600)
    from panel_core.jobs.payments import cleanup_old_payments

    with (
        app.app_context(),
        patch("panel_core.services.billing.yookassa.Payment.find_one") as mock_find,
        patch("panel_core.jobs.payments.bot_events.publish"),
    ):
        mock_find.return_value = SimpleNamespace(status="canceled")
        cleanup_old_payments()
        assert db.session.get(Payment, pid).status == "cancelled"


def test_cleanup_does_not_cancel_what_yookassa_reports_succeeded(app, tariff):

    pid = _insert_pending(app, tariff, age_seconds=25 * 3600, yk_id="yk-paid-late")
    from panel_core.jobs.payments import cleanup_old_payments

    with (
        app.app_context(),
        patch("panel_core.services.billing.yookassa.Payment.find_one") as mock_find,
        patch("panel_core.services.billing.provisioning.apply_tariff_for_user") as mock_provision,
        patch("panel_core.services.billing.bot_events.publish") as mock_billing_publish,
        patch("panel_core.jobs.payments.bot_events.publish") as mock_publish,
    ):
        mock_find.return_value = SimpleNamespace(status="succeeded")
        mock_provision.return_value = {"clients": [], "expires_at_ms": 9999999999000, "source": "yookassa"}
        cleanup_old_payments()

    with app.app_context():
        assert db.session.get(Payment, pid).status == "succeeded", (
            "cleanup_old_payments cancelled a payment YooKassa reports as succeeded — with no public "
            "webhook route and poll_pending_payments bounded to 24h, a >24h bot-api outage would turn "
            "every genuinely paid payment into 'cancelled' on restart"
        )
    published = [c.args[0] for c in mock_publish.call_args_list] + [
        c.args[0] for c in mock_billing_publish.call_args_list
    ]
    assert "payment_cancelled" not in published, f"a paid payment was announced as cancelled: {published}"
    assert mock_provision.call_count == 1, "the late-confirmed payment must be provisioned, not merely left alone"


def test_cleanup_leaves_the_row_pending_when_yookassa_is_unreachable(app, tariff):

    pid = _insert_pending(app, tariff, age_seconds=25 * 3600, yk_id="yk-unreachable")
    from panel_core.jobs.payments import cleanup_old_payments

    with (
        app.app_context(),
        patch("panel_core.services.billing.yookassa.Payment.find_one", side_effect=RuntimeError("network down")),
        patch("panel_core.jobs.payments.bot_events.publish") as mock_publish,
    ):
        cleanup_old_payments()

    with app.app_context():
        assert db.session.get(Payment, pid).status == "pending", (
            "cleanup_old_payments cancelled a payment it could not check — an unreachable YooKassa is "
            "not evidence that the user did not pay; skipping is safe, cancelling is not"
        )
    assert mock_publish.call_args_list == [], "no cancellation may be announced for an unverified payment"


def test_cleanup_leaves_money_holding_statuses_pending(app, tariff):

    pid = _insert_pending(app, tariff, age_seconds=25 * 3600, yk_id="yk-held")
    from panel_core.jobs.payments import cleanup_old_payments

    with (
        app.app_context(),
        patch("panel_core.services.billing.yookassa.Payment.find_one") as mock_find,
        patch("panel_core.jobs.payments.bot_events.publish") as mock_publish,
    ):
        mock_find.return_value = SimpleNamespace(status="waiting_for_capture")
        cleanup_old_payments()

    with app.app_context():
        assert db.session.get(Payment, pid).status == "pending"
    assert mock_publish.call_args_list == []


def test_cleanup_notifies_user_on_stuck_pending_cancellation(app, tariff):

    pid_a = _insert_pending(app, tariff, age_seconds=25 * 3600, yk_id="yk-stuck-a")
    pid_b = _insert_pending(app, tariff, age_seconds=25 * 3600, yk_id="yk-stuck-b")
    with app.app_context():
        for pid, chat, msg in [(pid_a, 100, 1), (pid_b, 200, 2)]:
            p = db.session.get(Payment, pid)
            p.chat_id = chat
            p.message_id = msg
        db.session.commit()

    from panel_core.jobs.payments import cleanup_old_payments

    with (
        app.app_context(),
        patch("panel_core.services.billing.yookassa.Payment.find_one") as mock_find,
        patch("panel_core.jobs.payments.bot_events.publish") as mock_publish,
    ):
        mock_find.return_value = SimpleNamespace(status="canceled")
        cleanup_old_payments()

    events = [c for c in mock_publish.call_args_list if c.args and c.args[0] == "payment_cancelled"]
    assert len(events) == 2
    payloads = sorted([c.args[2] for c in events], key=lambda p: p["payment_id"])
    assert payloads[0]["chat_id"] == 100
    assert payloads[1]["chat_id"] == 200


def test_cleanup_deletes_ancient_cancelled(app, tariff):
    pid = _insert_pending(app, tariff, age_seconds=91 * 86400)
    with app.app_context():
        db.session.get(Payment, pid).status = "cancelled"
        db.session.commit()
    from panel_core.jobs.payments import cleanup_old_payments

    with app.app_context():
        cleanup_old_payments()
        assert db.session.get(Payment, pid) is None
