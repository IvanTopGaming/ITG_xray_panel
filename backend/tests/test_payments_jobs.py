import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from panel_core.extensions import db
from panel_core.models import BotEvent, Payment, SystemSetting, Tariff, TariffItem
from panel_core.jobs import payments
from panel_core.services import billing


@pytest.fixture
def tariff(app):
    with app.app_context():
        db.session.add_all(
            [
                SystemSetting(key="yookassa_shop_id", value="shop"),
                SystemSetting(key="yookassa_secret_key", value="secret"),
            ]
        )
        row = Tariff(name="Standard", price_rub=150, period_days=30, visibility="public", enabled=True, is_trial=False)
        row.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        db.session.add(row)
        db.session.commit()
        yield row.id


def insert(app, tariff_id, *, status="pending", age=600, key="yk-payment", url="https://checkout", lease=None):
    with app.app_context():
        row = Payment(
            yookassa_id=key,
            telegram_id=42,
            tariff_id=tariff_id,
            tariff_snapshot={
                "name": "Purchased",
                "price_rub": 150,
                "period_days": 30,
                "items": [{"inbound_tag": "vless-de", "panel_id": None, "traffic_gb": 0}],
            },
            amount_rub=150,
            status=status,
            confirmation_url=url,
            metadata_json={"lang": "ru"},
            created_at=billing._now() - dt.timedelta(seconds=age),
        )
        if status == "succeeded":
            row.provider_status = "succeeded"
            row.fulfillment_status = "succeeded"
        if lease is not None:
            row.processing_owner = "worker"
            row.processing_version = 1
            row.processing_expires_at = billing._now() + dt.timedelta(seconds=lease)
            row.fulfillment_status = "processing"
        db.session.add(row)
        db.session.commit()
        return row.id


def remote(key, status="pending", refunded="0.00"):
    return SimpleNamespace(
        id=key,
        status=status,
        amount=SimpleNamespace(value="150.00", currency="RUB"),
        refunded_amount=SimpleNamespace(value=refunded, currency="RUB"),
        metadata={},
    )


@pytest.mark.parametrize("age", [600, 25 * 3600])
def test_poll_recovers_paid_money_including_old_invoices(app, tariff, age):
    pid = insert(app, tariff, age=age)
    with (
        app.app_context(),
        patch.object(billing.yookassa.Payment, "find_one", return_value=remote("yk-payment", "succeeded")),
        patch.object(billing.provisioning, "apply_tariff_for_user", return_value={"expires_at_ms": 123}),
    ):
        payments.poll_pending_payments()
        assert db.session.get(Payment, pid).fulfillment_status == "succeeded"
        assert BotEvent.query.filter_by(type="payment_succeeded").count() == 1


def test_poll_authoritative_cancel_event_carries_chat_coords(app, tariff):
    pid = insert(app, tariff)
    with (
        app.app_context(),
        patch.object(billing.yookassa.Payment, "find_one", return_value=remote("yk-payment", "canceled")),
    ):
        p = db.session.get(Payment, pid)
        p.chat_id, p.message_id = 42000, 555
        db.session.commit()
        payments.poll_pending_payments()
        assert p.status == "cancelled"
        event = BotEvent.query.filter_by(type="payment_cancelled").one()
        assert event.payload["chat_id"] == 42000
        assert event.payload["message_id"] == 555


def test_poll_skips_payments_younger_than_30s(app, tariff):
    pid = insert(app, tariff, age=10)
    with app.app_context(), patch.object(billing.yookassa.Payment, "find_one", side_effect=AssertionError("too early")):
        payments.poll_pending_payments()
        assert db.session.get(Payment, pid).last_checked_at is None


def test_poll_swallows_individual_failures_and_advances_timestamp(app, tariff):
    first, second = insert(app, tariff, key="yk-a"), insert(app, tariff, key="yk-b")

    def find(key):
        if key == "yk-a":
            raise RuntimeError("offline")
        return remote(key, "succeeded")

    with (
        app.app_context(),
        patch.object(billing.yookassa.Payment, "find_one", side_effect=find),
        patch.object(billing.provisioning, "apply_tariff_for_user", return_value={"expires_at_ms": 123}),
    ):
        payments.poll_pending_payments()
        assert db.session.get(Payment, first).status == "pending"
        assert db.session.get(Payment, first).last_checked_at is not None
        assert db.session.get(Payment, second).status == "succeeded"


@pytest.mark.parametrize("days", [1, 31, 365])
def test_refund_reconciliation_covers_old_paid_invoices(app, tariff, days):
    pid = insert(app, tariff, status="succeeded", age=days * 86400)
    with (
        app.app_context(),
        patch.object(billing.yookassa.Payment, "find_one", return_value=remote("yk-payment", "succeeded", "150.00")),
        patch.object(billing.provisioning, "revoke_payment_access", return_value={"panel_failures": []}) as revoke,
    ):
        payments.reconcile_refunds()
        assert db.session.get(Payment, pid).refund_status == "completed"
        assert revoke.call_args.kwargs["operation_id"] == f"pay:{pid}"
        assert BotEvent.query.filter_by(type="payment_refunded").count() == 1


@pytest.mark.parametrize("amount", ["0.00", "1.00"])
def test_no_access_revocation_for_zero_or_partial_refund(app, tariff, amount):
    pid = insert(app, tariff, status="succeeded")
    with (
        app.app_context(),
        patch.object(billing.yookassa.Payment, "find_one", return_value=remote("yk-payment", "succeeded", amount)),
        patch.object(billing.provisioning, "revoke_payment_access") as revoke,
    ):
        payments.reconcile_refunds()
        assert db.session.get(Payment, pid).status == "succeeded"
        assert db.session.get(Payment, pid).refunded_amount_kopeks == (100 if amount == "1.00" else 0)
        revoke.assert_not_called()
        assert BotEvent.query.count() == 0


@pytest.mark.parametrize("status", ["pending", "waiting_for_capture", "canceled", "succeeded"])
def test_cleanup_uses_only_authoritative_provider_state(app, tariff, status):
    pid = insert(app, tariff, age=25 * 3600)
    with (
        app.app_context(),
        patch.object(billing.yookassa.Payment, "find_one", return_value=remote("yk-payment", status)),
        patch.object(billing.provisioning, "apply_tariff_for_user", return_value={"expires_at_ms": 123}),
    ):
        payments.cleanup_old_payments()
        expected = {
            "pending": "pending",
            "waiting_for_capture": "pending",
            "canceled": "cancelled",
            "succeeded": "succeeded",
        }[status]
        assert db.session.get(Payment, pid).status == expected
        assert BotEvent.query.filter_by(type="payment_cancelled").count() == (1 if status == "canceled" else 0)


def test_cleanup_keeps_unverifiable_old_payment_and_does_not_notify_cancel(app, tariff):
    pid = insert(app, tariff, age=91 * 86400, status="cancelled")
    with app.app_context(), patch.object(billing.yookassa.Payment, "find_one", side_effect=RuntimeError("offline")):
        payments.cleanup_old_payments()
        assert db.session.get(Payment, pid) is not None
        assert db.session.get(Payment, pid).provider_status == "pending"
        assert BotEvent.query.count() == 0


def test_cleanup_preserves_unknown_creation_for_review(app, tariff):
    pid = insert(app, tariff, age=25 * 3600, key="pending-placeholder", url=None)
    with app.app_context(), patch.object(billing.yookassa.Payment, "create") as create:
        payments.cleanup_old_payments()
        p = db.session.get(Payment, pid)
        assert p.status == "pending"
        assert p.checkout_status == "review"
        assert p.fulfillment_error == "checkout_request_missing"
        create.assert_not_called()


def test_cleanup_marks_every_confirmed_cancellation_with_its_own_chat(app, tariff):
    first, second = insert(app, tariff, age=25 * 3600, key="a"), insert(app, tariff, age=25 * 3600, key="b")
    with (
        app.app_context(),
        patch.object(billing.yookassa.Payment, "find_one", side_effect=lambda key: remote(key, "canceled")),
    ):
        for pid, chat in [(first, 100), (second, 200)]:
            db.session.get(Payment, pid).chat_id = chat
        db.session.commit()
        payments.cleanup_old_payments()
        assert {event.payload["chat_id"] for event in BotEvent.query.filter_by(type="payment_cancelled")} == {100, 200}


def test_cleanup_prioritizes_least_recently_checked_invoices(app, tariff):
    first, second = (
        insert(app, tariff, age=25 * 3600, key="recent"),
        insert(app, tariff, age=72 * 3600, key="unchecked"),
    )
    seen = []
    with (
        app.app_context(),
        patch.object(billing.yookassa.Payment, "find_one", side_effect=lambda key: seen.append(key) or remote(key)),
    ):
        db.session.get(Payment, first).last_checked_at = billing._now()
        db.session.commit()
        payments.cleanup_old_payments()
        assert seen == ["unchecked", "recent"]
        assert db.session.get(Payment, second).last_checked_at is not None


@pytest.mark.parametrize("job", [payments.poll_pending_payments, payments.cleanup_old_payments])
def test_active_lease_protects_old_processing_payment(app, tariff, job):
    pid = insert(app, tariff, status="processing", age=72 * 3600, lease=120)
    with (
        app.app_context(),
        patch.object(billing.yookassa.Payment, "find_one", side_effect=AssertionError("live owner")),
    ):
        job()
        assert db.session.get(Payment, pid).status == "processing"
        assert db.session.get(Payment, pid).processing_owner == "worker"


@pytest.mark.parametrize("job", [payments.poll_pending_payments, payments.cleanup_old_payments])
def test_expired_lease_is_recovered_by_both_jobs(app, tariff, job):
    pid = insert(app, tariff, status="processing", age=72 * 3600, lease=-1)
    with (
        app.app_context(),
        patch.object(billing.yookassa.Payment, "find_one", return_value=remote("yk-payment", "succeeded")),
        patch.object(billing.provisioning, "apply_tariff_for_user", return_value={"expires_at_ms": 123}),
    ):
        job()
        assert db.session.get(Payment, pid).status == "succeeded"
        assert db.session.get(Payment, pid).processing_owner is None
