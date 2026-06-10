from __future__ import annotations

import datetime as dt
import logging

import gevent
import yookassa
from yookassa import Configuration

from app.extensions import db
from app.models import Payment, SystemSetting
from app.services import billing, bot_events

logger = logging.getLogger(__name__)

_MIN_AGE_S = 30
_MAX_AGE_S = 24 * 3600


_YK_CALL_TIMEOUT_S = 8


_REFUND_LOOKBACK_DAYS = 30
_REFUND_BATCH = 200


def _get_setting(key: str) -> str:
    row = SystemSetting.query.filter_by(key=key).first()
    return row.value if row and row.value else ""


def _configure_sdk() -> bool:
    shop_id = _get_setting("yookassa_shop_id")
    secret = _get_setting("yookassa_secret_key")
    if not shop_id or not secret:
        return False
    Configuration.account_id = shop_id
    Configuration.secret_key = secret
    return True


def poll_pending_payments() -> None:
    if not _configure_sdk():
        return

    now = dt.datetime.utcnow()
    lo = now - dt.timedelta(seconds=_MAX_AGE_S)
    hi = now - dt.timedelta(seconds=_MIN_AGE_S)
    pending = (
        Payment.query.filter(Payment.status == "pending")
        .filter(Payment.created_at >= lo)
        .filter(Payment.created_at <= hi)
        .limit(200)
        .all()
    )

    for payment in pending:
        try:
            yk = gevent.with_timeout(
                _YK_CALL_TIMEOUT_S,
                yookassa.Payment.find_one,
                payment.yookassa_id,
            )
        except gevent.Timeout:
            logger.info(
                "poll_pending_payments: yookassa find_one timed out after %ss payment=%s yk=%s",
                _YK_CALL_TIMEOUT_S,
                payment.id,
                payment.yookassa_id,
            )
            continue
        except Exception as exc:
            logger.info(
                "poll_pending_payments: lookup failed payment=%s yk=%s err=%s",
                payment.id,
                payment.yookassa_id,
                exc,
            )
            continue
        try:
            if yk.status == "succeeded":
                billing.apply_payment(payment)
            elif yk.status == "canceled":
                payment.status = "cancelled"
                db.session.commit()
                bot_events.publish(
                    "payment_cancelled",
                    payment.telegram_id,
                    {
                        "payment_id": payment.id,
                        "lang": (payment.metadata_json or {}).get("lang", "ru"),
                        "chat_id": payment.chat_id,
                        "message_id": payment.message_id,
                    },
                )
        except Exception:
            logger.exception(
                "poll_pending_payments: apply failed payment=%s",
                payment.id,
            )


def reconcile_refunds() -> None:

    if not _configure_sdk():
        return

    now = dt.datetime.utcnow()
    lo = now - dt.timedelta(days=_REFUND_LOOKBACK_DAYS)
    candidates = (
        Payment.query.filter(Payment.status == "succeeded")
        .filter(Payment.created_at >= lo)
        .order_by(Payment.created_at.desc())
        .limit(_REFUND_BATCH)
        .all()
    )
    if len(candidates) >= _REFUND_BATCH:
        logger.info(
            "reconcile_refunds: hit batch cap (%d); older succeeded payments not checked this run",
            _REFUND_BATCH,
        )

    for payment in candidates:
        try:
            billing.handle_refund(payment)
        except Exception:
            logger.exception("reconcile_refunds: handle_refund failed payment=%s", payment.id)


def cleanup_old_payments() -> None:

    now = dt.datetime.utcnow()
    stuck = (
        Payment.query.filter(
            Payment.status == "pending",
            Payment.created_at < now - dt.timedelta(hours=24),
        )
        .limit(500)
        .all()
    )
    for payment in stuck:
        payment.status = "cancelled"
    db.session.commit()
    for payment in stuck:
        try:
            bot_events.publish(
                "payment_cancelled",
                payment.telegram_id,
                {
                    "payment_id": payment.id,
                    "lang": (payment.metadata_json or {}).get("lang", "ru"),
                    "chat_id": payment.chat_id,
                    "message_id": payment.message_id,
                },
            )
        except Exception:
            logger.exception("cleanup_old_payments: notify failed payment=%s", payment.id)

    Payment.query.filter(
        Payment.status.in_(["cancelled", "failed"]),
        Payment.created_at < now - dt.timedelta(days=90),
    ).delete(synchronize_session=False)
    db.session.commit()
