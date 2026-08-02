from __future__ import annotations

import datetime as dt
import logging
import time

import gevent
import yookassa
from yookassa import Configuration

from sqlalchemy import update

from panel_core.extensions import db
from panel_core.models import Payment, SystemSetting
from panel_core.services import billing, bot_events

logger = logging.getLogger(__name__)

_MIN_AGE_S = 30
_MAX_AGE_S = 24 * 3600

_STRANDED_AFTER_S = 120
_seen_processing: dict[int, float] = {}


_YK_CALL_TIMEOUT_S = 8


_REFUND_LOOKBACK_DAYS = 30
_REFUND_BATCH = 200


_UNCANCELLABLE_REMOTE_STATUSES = frozenset({"succeeded", "waiting_for_capture"})


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


def release_stranded_claims() -> None:
    """§23/§8.16: a payment left in 'processing' by a crash gets back into the queue in minutes.

    `apply_payment` claims a payment with `UPDATE … WHERE status='pending'` and that claim is the
    only thing standing between two hosts and a double grant, so it must NOT be widened to include
    'processing'. Recovery is this separate branch instead: put the row back to 'pending' and let
    the ordinary poll take it on the next tick.

    **How long it has been claimed is not written down anywhere.** There is no `updated_at` on
    `Payment`, and adding a column to an existing table is the one thing the Postgres migration path
    cannot do (§40). `created_at` is the wrong clock -- the poll reaches back 24 hours, so a payment
    can be claimed long after it was created. So the age is measured here, in the process that runs
    the job: a payment seen in 'processing' twice, `_STRANDED_AFTER_S` apart, is stranded. All three
    claim holders (the webhook, this poll, the cleanup cron) live in the same bot-api process, and
    the case being recovered from -- that process dying -- is also what clears this map, which costs
    one extra cycle and never a wrong release.

    Releasing a claim that is somehow still in flight is survivable rather than merely unlikely:
    `operation_id` is `pay:<payment_id>`, so a node that already granted replays its stored receipt
    and adds nothing (wave 3a). The visible cost would be a second "payment received" message.
    """

    now = time.monotonic()
    stranded = Payment.query.filter(Payment.status == "processing").limit(200).all()
    live = {payment.id for payment in stranded}
    for gone in [pid for pid in _seen_processing if pid not in live]:
        _seen_processing.pop(gone, None)

    for payment in stranded:
        first_seen = _seen_processing.setdefault(payment.id, now)
        if now - first_seen < _STRANDED_AFTER_S:
            continue
        released = db.session.execute(
            update(Payment).where(Payment.id == payment.id, Payment.status == "processing").values(status="pending")
        )
        db.session.commit()
        if released.rowcount:
            _seen_processing.pop(payment.id, None)
            logger.warning(
                "release_stranded_claims: payment=%s yk=%s sat in 'processing' for over %ss — the process "
                "that claimed it did not finish. Released back to 'pending'; the poll will retry it.",
                payment.id,
                payment.yookassa_id,
                _STRANDED_AFTER_S,
            )


def poll_pending_payments() -> None:
    if not _configure_sdk():
        return

    release_stranded_claims()

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
            Payment.status.in_(("pending", "processing")),
            Payment.created_at < now - dt.timedelta(hours=24),
        )
        .order_by(Payment.created_at.asc())
        .limit(500)
        .all()
    )
    cancelled = []
    for payment in stuck:
        if payment.status == "processing":
            logger.warning(
                "cleanup_old_payments: payment=%s yk=%s was stranded in 'processing' — a crash between the "
                "atomic claim and the end of apply_payment; releasing it back to pending",
                payment.id,
                payment.yookassa_id,
            )
            payment.status = "pending"
            db.session.commit()
        if payment.confirmation_url is None:
            logger.info(
                "cleanup_old_payments: payment=%s yk=%s never reached a yookassa checkout page; cancelling locally",
                payment.id,
                payment.yookassa_id,
            )
            payment.status = "cancelled"
            cancelled.append(payment)
            continue
        remote = billing.fetch_remote_status(payment)
        if remote is None:
            logger.warning(
                "cleanup_old_payments: yookassa unreachable, leaving payment=%s yk=%s pending",
                payment.id,
                payment.yookassa_id,
            )
            continue
        if remote == "succeeded":
            logger.warning(
                "cleanup_old_payments: payment=%s yk=%s is succeeded at yookassa but still pending here; applying",
                payment.id,
                payment.yookassa_id,
            )
            try:
                billing.apply_payment(payment)
            except Exception:
                logger.exception("cleanup_old_payments: apply failed payment=%s", payment.id)
            continue
        if remote in _UNCANCELLABLE_REMOTE_STATUSES:
            logger.info(
                "cleanup_old_payments: payment=%s yk=%s holds money at yookassa (status=%s); leaving pending",
                payment.id,
                payment.yookassa_id,
                remote,
            )
            continue
        payment.status = "cancelled"
        cancelled.append(payment)
    db.session.commit()
    for payment in cancelled:
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
