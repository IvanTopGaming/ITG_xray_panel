from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import or_, update

from panel_core.extensions import db
from panel_core.models import Payment
from panel_core.services import billing

logger = logging.getLogger(__name__)
_MIN_AGE_S = 30
_REFUND_BATCH = 200


def release_stranded_claims():
    now = billing._now()
    for payment in (
        Payment.query.filter(Payment.processing_owner.isnot(None), Payment.processing_expires_at <= now)
        .order_by(Payment.processing_expires_at, Payment.id)
        .limit(200)
        .all()
    ):
        values = {"processing_owner": None, "processing_expires_at": None}
        if payment.fulfillment_status == "processing":
            values.update(fulfillment_status="retry", status="pending")
        if payment.refund_status == "processing":
            values.update(refund_status="pending")
        db.session.execute(
            update(Payment)
            .where(
                Payment.id == payment.id,
                Payment.processing_owner == payment.processing_owner,
                Payment.processing_version == payment.processing_version,
                Payment.processing_expires_at <= now,
            )
            .values(**values)
        )
    db.session.commit()


def _poll_batch(limit, *, old_only=False):
    release_stranded_claims()
    query = Payment.query.filter(
        Payment.processing_owner.is_(None),
        Payment.checkout_status != "review",
        or_(
            Payment.provider_status.notin_(("canceled", "succeeded")),
            (Payment.provider_status == "succeeded") & Payment.fulfillment_status.in_(("pending", "retry", "blocked")),
        ),
        Payment.status != "refunded",
        Payment.created_at <= billing._now() - dt.timedelta(seconds=_MIN_AGE_S),
    )
    if old_only:
        query = query.filter(Payment.created_at < billing._now() - dt.timedelta(hours=24))
    candidates = query.order_by(Payment.last_checked_at.asc().nullsfirst(), Payment.id).limit(limit).all()
    for payment in candidates:
        payment.last_checked_at = billing._now()
        db.session.commit()
        try:
            if payment.checkout_status == "creating" or payment.yookassa_id.startswith("pending-"):
                billing.resume_checkout(payment)
            if payment.provider_status == "succeeded":
                billing.apply_payment(payment)
                continue
            status = billing.fetch_remote_status(payment)
            if status:
                billing.accept_remote_status(payment, status)
        except Exception:
            db.session.rollback()
            logger.exception("Payment reconciliation failed payment=%s", payment.id)


def poll_pending_payments():
    _poll_batch(200)


def reconcile_refunds():
    release_stranded_claims()
    candidates = (
        Payment.query.filter(
            or_(Payment.provider_status == "succeeded", Payment.status.in_(("succeeded", "refunded"))),
            Payment.refund_status != "completed",
            Payment.processing_owner.is_(None),
        )
        .order_by(Payment.refund_checked_at.asc().nullsfirst(), Payment.id)
        .limit(_REFUND_BATCH)
        .all()
    )
    for payment in candidates:
        payment.refund_checked_at = billing._now()
        db.session.commit()
        try:
            billing.handle_refund(payment)
        except Exception:
            db.session.rollback()
            logger.exception("Refund reconciliation failed payment=%s", payment.id)


def cleanup_old_payments():
    _poll_batch(500, old_only=True)
