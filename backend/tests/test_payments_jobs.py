import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Payment, SystemSetting, Tariff, TariffItem


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
    from app.jobs.payments import poll_pending_payments

    with (
        app.app_context(),
        patch("app.jobs.payments.yookassa.Payment.find_one") as mock_find,
        patch("app.services.billing.provisioning.apply_tariff_for_user") as mock_provision,
        patch("app.services.billing.bot_events.publish"),
    ):
        mock_find.return_value = SimpleNamespace(status="succeeded")
        mock_provision.return_value = {"clients": [], "expires_at_ms": 9999999999000, "source": "yookassa"}
        poll_pending_payments()
    with app.app_context():
        assert Payment.query.get(pid).status == "succeeded"


def test_poll_marks_cancelled(app, tariff):
    pid = _insert_pending(app, tariff)
    from app.jobs.payments import poll_pending_payments

    with (
        app.app_context(),
        patch("app.jobs.payments.yookassa.Payment.find_one") as mock_find,
        patch("app.services.billing.bot_events.publish") as mock_publish,
        patch("app.jobs.payments.bot_events.publish") as mock_publish_local,
    ):
        mock_find.return_value = SimpleNamespace(status="canceled")
        poll_pending_payments()
    with app.app_context():
        assert Payment.query.get(pid).status == "cancelled"
    # The publish call lives in app.jobs.payments — check that name first; fall back to billing's
    assert (mock_publish_local.call_args or mock_publish.call_args).args[0] == "payment_cancelled"


def test_poll_cancel_event_carries_chat_coords(app, tariff):
    """When the poll cron flips a payment to cancelled it must include
    chat_id/message_id in the payload — same as the webhook path — so the
    consumer can delete the stale checkout bubble."""
    pid = _insert_pending(app, tariff)
    with app.app_context():
        p = Payment.query.get(pid)
        p.chat_id = 42_000
        p.message_id = 555
        db.session.commit()

    from app.jobs.payments import poll_pending_payments

    with (
        app.app_context(),
        patch("app.jobs.payments.yookassa.Payment.find_one") as mock_find,
        patch("app.jobs.payments.bot_events.publish") as mock_publish,
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
    from app.jobs.payments import poll_pending_payments

    with app.app_context(), patch("app.jobs.payments.yookassa.Payment.find_one") as mock_find:
        mock_find.return_value = SimpleNamespace(status="succeeded")
        poll_pending_payments()
    mock_find.assert_not_called()
    with app.app_context():
        assert Payment.query.get(pid).status == "pending"


def test_poll_skips_payments_older_than_24h(app, tariff):
    _insert_pending(app, tariff, age_seconds=25 * 3600)
    from app.jobs.payments import poll_pending_payments

    with app.app_context(), patch("app.jobs.payments.yookassa.Payment.find_one") as mock_find:
        mock_find.return_value = SimpleNamespace(status="succeeded")
        poll_pending_payments()
    mock_find.assert_not_called()


def test_poll_swallows_individual_failures(app, tariff):
    pid_a = _insert_pending(app, tariff, yk_id="yk-a")
    pid_b = _insert_pending(app, tariff, yk_id="yk-b")
    from app.jobs.payments import poll_pending_payments

    def find_side_effect(yk_id):
        if yk_id == "yk-a":
            raise RuntimeError("transient")
        return SimpleNamespace(status="succeeded")

    with (
        app.app_context(),
        patch("app.jobs.payments.yookassa.Payment.find_one", side_effect=find_side_effect),
        patch("app.services.billing.provisioning.apply_tariff_for_user") as mock_provision,
        patch("app.services.billing.bot_events.publish"),
    ):
        mock_provision.return_value = {"clients": [], "expires_at_ms": 9999999999000, "source": "yookassa"}
        poll_pending_payments()  # must not raise
    with app.app_context():
        assert Payment.query.get(pid_a).status == "pending"
        assert Payment.query.get(pid_b).status == "succeeded"


def test_cleanup_cancels_stale_pending(app, tariff):
    pid = _insert_pending(app, tariff, age_seconds=25 * 3600)
    from app.jobs.payments import cleanup_old_payments

    with app.app_context(), patch("app.jobs.payments.bot_events.publish"):
        cleanup_old_payments()
        assert Payment.query.get(pid).status == "cancelled"


def test_cleanup_notifies_user_on_stuck_pending_cancellation(app, tariff):
    """When the 24h cleanup cancels a stuck pending payment the user must
    be told via payment_cancelled — otherwise they're stuck staring at
    a 'pay here' bubble that silently became a dead link."""
    pid_a = _insert_pending(app, tariff, age_seconds=25 * 3600, yk_id="yk-stuck-a")
    pid_b = _insert_pending(app, tariff, age_seconds=25 * 3600, yk_id="yk-stuck-b")
    with app.app_context():
        for pid, chat, msg in [(pid_a, 100, 1), (pid_b, 200, 2)]:
            p = Payment.query.get(pid)
            p.chat_id = chat
            p.message_id = msg
        db.session.commit()

    from app.jobs.payments import cleanup_old_payments

    with app.app_context(), patch("app.jobs.payments.bot_events.publish") as mock_publish:
        cleanup_old_payments()

    events = [c for c in mock_publish.call_args_list if c.args and c.args[0] == "payment_cancelled"]
    assert len(events) == 2
    payloads = sorted([c.args[2] for c in events], key=lambda p: p["payment_id"])
    assert payloads[0]["chat_id"] == 100
    assert payloads[1]["chat_id"] == 200


def test_cleanup_deletes_ancient_cancelled(app, tariff):
    pid = _insert_pending(app, tariff, age_seconds=91 * 86400)
    with app.app_context():
        Payment.query.get(pid).status = "cancelled"
        db.session.commit()
    from app.jobs.payments import cleanup_old_payments

    with app.app_context():
        cleanup_old_payments()
        assert Payment.query.get(pid) is None
