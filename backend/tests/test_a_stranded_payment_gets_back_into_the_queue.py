import datetime as dt
from unittest.mock import patch

import pytest

from panel_core.extensions import db
from panel_core.models import BotEvent, Payment
from panel_core.jobs import payments
from panel_core.services import billing


@pytest.fixture
def stranded(app):
    with app.app_context():
        row = Payment(
            yookassa_id="yk-stranded",
            telegram_id=42,
            tariff_id=1,
            tariff_snapshot={
                "name": "Purchased",
                "period_days": 30,
                "items": [{"inbound_tag": "vpn", "panel_id": 1, "traffic_gb": 0}],
            },
            amount_rub=150,
            status="processing",
            provider_status="succeeded",
            fulfillment_status="processing",
            processing_owner="dead-worker",
            processing_version=1,
            processing_expires_at=billing._now() + dt.timedelta(seconds=120),
            created_at=billing._now() - dt.timedelta(days=2),
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def test_live_claim_is_left_alone_even_after_process_restart(app, stranded):
    with app.app_context():
        payments.release_stranded_claims()
        db.session.remove()
        payments.release_stranded_claims()
        assert db.session.get(Payment, stranded).processing_owner == "dead-worker"


def test_expired_persisted_claim_returns_to_queue(app, stranded):
    with app.app_context():
        row = db.session.get(Payment, stranded)
        row.processing_expires_at = billing._now() - dt.timedelta(seconds=1)
        db.session.commit()
        db.session.remove()
        payments.release_stranded_claims()
        row = db.session.get(Payment, stranded)
        assert row.status == "pending"
        assert row.fulfillment_status == "retry"
        assert row.processing_owner is None


@pytest.mark.parametrize("job", [payments.poll_pending_payments, payments.cleanup_old_payments])
def test_expired_paid_claim_is_delivered_by_recovery_jobs(app, stranded, job):
    with app.app_context():
        row = db.session.get(Payment, stranded)
        row.processing_expires_at = billing._now() - dt.timedelta(seconds=1)
        db.session.commit()
        with patch.object(billing.provisioning, "apply_tariff_for_user", return_value={"expires_at_ms": 123}):
            job()
        assert row.status == "succeeded"
        assert row.processing_owner is None
        assert BotEvent.query.filter_by(type="payment_succeeded").count() == 1


def test_second_handler_cannot_take_a_live_processing_claim(app, stranded):
    with app.app_context(), patch.object(billing.provisioning, "apply_tariff_for_user") as provision:
        billing.apply_payment(db.session.get(Payment, stranded))
        provision.assert_not_called()
        assert db.session.get(Payment, stranded).processing_owner == "dead-worker"
        assert BotEvent.query.count() == 0


def test_expired_refund_claim_returns_to_its_own_queue(app, stranded):
    with app.app_context():
        row = db.session.get(Payment, stranded)
        row.status, row.fulfillment_status, row.refund_status = "refunded", "succeeded", "processing"
        row.processing_expires_at = billing._now() - dt.timedelta(seconds=1)
        db.session.commit()
        payments.release_stranded_claims()
        assert row.status == "refunded"
        assert row.refund_status == "pending"
        assert row.fulfillment_status == "succeeded"
        assert row.processing_owner is None
