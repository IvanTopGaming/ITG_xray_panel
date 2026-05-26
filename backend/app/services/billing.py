"""YooKassa checkout + payment provisioning. The only module that talks to the YooKassa SDK."""

from __future__ import annotations

import datetime as _dt
import logging
import uuid
from typing import Any, Dict

import gevent
import yookassa
from sqlalchemy import update
from yookassa import Configuration

from app.extensions import db
from app.models import Payment, SystemSetting, Tariff, UserTariffAccess
from app.services import bot_events, provisioning

logger = logging.getLogger(__name__)

# Hard cap on a single YooKassa SDK call. The SDK uses `requests` with no
# read timeout — under gevent monkey-patching a stuck remote hangs the
# greenlet indefinitely. Wrap every SDK call so user-facing checkouts and
# background polls always release the worker.
_YK_CALL_TIMEOUT_S = 8


def _get_setting(key: str) -> str:
    row = SystemSetting.query.filter_by(key=key).first()
    return row.value if row and row.value else ""


def _configure_sdk() -> None:
    shop_id = _get_setting("yookassa_shop_id")
    secret = _get_setting("yookassa_secret_key")
    if not shop_id or not secret:
        raise ValueError("yookassa_not_configured")
    Configuration.account_id = shop_id
    Configuration.secret_key = secret


def _build_snapshot(tariff: Tariff) -> Dict[str, Any]:
    return {
        "name": tariff.name,
        "price_rub": tariff.price_rub,
        "period_days": tariff.period_days,
        "visibility": tariff.visibility,
        "is_trial": tariff.is_trial,
        "items": [
            {
                "inbound_tag": item.inbound_tag,
                "traffic_gb": item.traffic_gb,
                "panel_id": item.panel_id,
            }
            for item in tariff.items
        ],
    }


def _ensure_tariff_available(tariff: Tariff | None, telegram_id: int) -> None:
    if tariff is None:
        raise ValueError("tariff_not_available")
    if not tariff.enabled or tariff.visibility == "archived" or tariff.is_trial:
        raise ValueError("tariff_not_available")
    if tariff.visibility == "private":
        grant = UserTariffAccess.query.filter_by(telegram_id=telegram_id, tariff_id=tariff.id).first()
        if grant is None:
            raise ValueError("tariff_not_available")


def create_checkout(*, telegram_id: int, tariff_id: int, lang: str) -> Dict[str, Any]:
    tariff = Tariff.query.get(tariff_id)
    _ensure_tariff_available(tariff, telegram_id)

    payment = Payment(
        yookassa_id=f"pending-{uuid.uuid4().hex}",  # placeholder, overwritten after API call
        telegram_id=telegram_id,
        tariff_id=tariff.id,
        tariff_snapshot=_build_snapshot(tariff),
        amount_rub=tariff.price_rub,
        status="pending",
        metadata_json={"telegram_id": telegram_id, "tariff_id": tariff.id, "lang": lang},
    )
    db.session.add(payment)
    db.session.flush()  # need payment.id for metadata

    _configure_sdk()

    return_url = _get_setting("yookassa_return_url") or "https://t.me/"
    # No `receipt` field — merchant uses "Чеки самозанятого" mode in YooKassa
    # cabinet, so YooKassa auto-generates fiscal receipts via the "Мой налог"
    # FNS integration. If the merchant later switches to ИП/ООО with an
    # online cash register (54-ФЗ), bring the `receipt` block back.
    payload = {
        "amount": {"value": f"{tariff.price_rub:.2f}", "currency": "RUB"},
        "description": f"{tariff.id}-{telegram_id}",
        "confirmation": {"type": "redirect", "return_url": return_url},
        "capture": True,
        "metadata": {
            "payment_db_id": payment.id,
            "telegram_id": telegram_id,
            "tariff_id": tariff.id,
        },
    }
    # YooKassa SDK uses urllib + ssl directly. Under gevent monkey-patching,
    # the TLS handshake to api.yookassa.ru occasionally hangs (seen in the
    # gevent.ssl.do_handshake greenlet for tens of seconds). Wrap in a
    # per-call timeout and retry once with the SAME idempotence key — if the
    # remote already accepted the first attempt, the retry returns the same
    # payment instead of double-creating.
    idempotence_key = uuid.uuid4().hex
    yk_payment = None
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            yk_payment = gevent.with_timeout(
                _YK_CALL_TIMEOUT_S,
                yookassa.Payment.create,
                payload,
                idempotence_key,
            )
            break
        except gevent.Timeout as exc:
            last_exc = exc
            logger.warning(
                "create_checkout: yookassa.Payment.create timed out after %ss (attempt %d)",
                _YK_CALL_TIMEOUT_S,
                attempt + 1,
            )
    if yk_payment is None:
        raise RuntimeError(f"yookassa_timeout after {_YK_CALL_TIMEOUT_S}s x2") from last_exc

    payment.yookassa_id = yk_payment.id
    payment.confirmation_url = yk_payment.confirmation.confirmation_url
    db.session.commit()

    logger.info(
        "billing.checkout created payment_id=%s yk=%s tg=%s tariff=%s",
        payment.id,
        yk_payment.id,
        telegram_id,
        tariff.id,
    )
    return {
        "payment_id": payment.id,
        "yookassa_id": yk_payment.id,
        "confirmation_url": yk_payment.confirmation.confirmation_url,
        "amount_rub": tariff.price_rub,
    }


def apply_payment(payment: Payment) -> None:
    """Provision the user and mark the payment succeeded. Idempotent.

    Webhook and poll cron can both reach this with a "pending" view of the
    same row; the atomic UPDATE below ensures only one crosses into
    provisioning.
    """
    if payment.status == "succeeded":
        return

    claim = db.session.execute(
        update(Payment).where(Payment.id == payment.id, Payment.status == "pending").values(status="processing")
    )
    db.session.commit()
    if claim.rowcount == 0:
        return

    tariff = Tariff.query.get(payment.tariff_id)

    rejected = False
    if tariff is None or not tariff.items:
        rejected = True
    else:
        try:
            _ensure_tariff_available(tariff, payment.telegram_id)
        except ValueError:
            rejected = True

    if rejected:
        payment.status = "failed"
        db.session.commit()
        bot_events.publish(
            "payment_failed",
            payment.telegram_id,
            {
                "payment_id": payment.id,
                "reason": "tariff_unavailable",
                "lang": (payment.metadata_json or {}).get("lang", "ru"),
                "chat_id": payment.chat_id,
                "message_id": payment.message_id,
            },
        )
        logger.warning(
            "billing.apply_payment marked failed: payment=%s tariff=%s no longer available",
            payment.id,
            payment.tariff_id,
        )
        return

    try:
        result = provisioning.apply_tariff_for_user(
            telegram_id=payment.telegram_id,
            tariff=tariff,
            source="yookassa",
        )
    except Exception:
        # Release the claim — poll cron only retries rows still in "pending".
        db.session.rollback()
        db.session.execute(update(Payment).where(Payment.id == payment.id).values(status="pending"))
        db.session.commit()
        raise

    payment.status = "succeeded"
    payment.paid_at = _dt.datetime.utcnow()
    db.session.commit()

    bot_events.publish(
        "payment_succeeded",
        payment.telegram_id,
        {
            "payment_id": payment.id,
            "tariff_id": tariff.id,
            "tariff_name": tariff.name,
            "amount_rub": payment.amount_rub,
            "expires_at_ms": result["expires_at_ms"],
            "lang": (payment.metadata_json or {}).get("lang", "ru"),
            "chat_id": payment.chat_id,
            "message_id": payment.message_id,
        },
    )
    logger.info(
        "billing.apply_payment succeeded: payment=%s tg=%s tariff=%s",
        payment.id,
        payment.telegram_id,
        tariff.id,
    )
