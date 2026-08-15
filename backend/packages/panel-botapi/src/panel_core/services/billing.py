from __future__ import annotations

import datetime as _dt
import logging
import uuid
from typing import Any, Dict

import gevent
import yookassa
from sqlalchemy import update
from yookassa import Configuration

from panel_core.extensions import db
from panel_core.models import Payment, SystemSetting, Tariff, UserTariffAccess
from panel_core.services.open_access import has_open_ended_access
from panel_core.services import bot_events, provisioning, tariff_delivery
from panel_core.xray.gateway import LocalXrayUnavailable

logger = logging.getLogger(__name__)


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


def _ensure_tariff_available(
    tariff: Tariff | None,
    telegram_id: int,
    *,
    where: str = "billing",
    check_open_ended: bool = True,
) -> None:
    if tariff is None:
        raise ValueError("tariff_not_available")
    if check_open_ended and has_open_ended_access(telegram_id):
        raise ValueError("open_ended_access")
    if not tariff.enabled or tariff.visibility == "archived" or tariff.is_trial:
        raise ValueError("tariff_not_available")
    if tariff.visibility == "private":
        grant = UserTariffAccess.query.filter_by(telegram_id=telegram_id, tariff_id=tariff.id).first()
        if grant is None:
            raise ValueError("tariff_not_available")
    if not tariff_delivery.is_deliverable(tariff):
        tariff_delivery.log_undeliverable(tariff, where)
        raise ValueError("tariff_not_available")


def create_checkout(*, telegram_id: int, tariff_id: int, lang: str) -> Dict[str, Any]:
    tariff = db.session.get(Tariff, tariff_id)
    _ensure_tariff_available(tariff, telegram_id, where="billing.create_checkout")

    _configure_sdk()
    return_url = _get_setting("yookassa_return_url") or "https://t.me/"

    payment = Payment(
        yookassa_id=f"pending-{uuid.uuid4().hex}",
        telegram_id=telegram_id,
        tariff_id=tariff.id,
        tariff_snapshot=_build_snapshot(tariff),
        amount_rub=tariff.price_rub,
        status="pending",
        metadata_json={"telegram_id": telegram_id, "tariff_id": tariff.id, "lang": lang},
    )
    db.session.add(payment)

    db.session.commit()

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


def fetch_remote_status(payment: Payment) -> str | None:

    try:
        _configure_sdk()
    except ValueError:
        return None
    try:
        yk = gevent.with_timeout(_YK_CALL_TIMEOUT_S, yookassa.Payment.find_one, payment.yookassa_id)
    except gevent.Timeout:
        logger.warning("fetch_remote_status: find_one timed out payment=%s", payment.id)
        return None
    except Exception as exc:
        logger.info("fetch_remote_status: find_one failed payment=%s err=%s", payment.id, exc)
        return None
    return yk.status


def _amount_value(amount) -> float:
    try:
        return float(getattr(amount, "value", None) or 0)
    except (TypeError, ValueError):
        return 0.0


def handle_refund(payment: Payment) -> None:

    if payment.status == "refunded":
        return

    if payment.status != "succeeded":
        return
    try:
        _configure_sdk()
    except ValueError:
        return
    try:
        yk = gevent.with_timeout(_YK_CALL_TIMEOUT_S, yookassa.Payment.find_one, payment.yookassa_id)
    except gevent.Timeout:
        logger.warning("handle_refund: find_one timed out payment=%s", payment.id)
        return
    except Exception as exc:
        logger.info("handle_refund: find_one failed payment=%s err=%s", payment.id, exc)
        return

    refunded = _amount_value(getattr(yk, "refunded_amount", None))
    if refunded <= 0:
        return

    result = provisioning.revoke_payment_access(payment.telegram_id, payment.tariff_id)
    payment.status = "refunded"
    db.session.commit()
    bot_events.publish(
        "payment_refunded",
        payment.telegram_id,
        {
            "payment_id": payment.id,
            "tariff_id": payment.tariff_id,
            "lang": (payment.metadata_json or {}).get("lang", "ru"),
            "chat_id": payment.chat_id,
            "message_id": payment.message_id,
        },
    )
    logger.warning(
        "billing.handle_refund revoked access: payment=%s tg=%s tariff=%s refunded=%s of %s disabled=%d",
        payment.id,
        payment.telegram_id,
        payment.tariff_id,
        refunded,
        payment.amount_rub,
        result.get("disabled_clients", 0),
    )


def _fail_payment(payment: Payment, reason: str) -> None:

    payment.status = "failed"
    db.session.commit()
    bot_events.publish(
        "payment_failed",
        payment.telegram_id,
        {
            "payment_id": payment.id,
            "reason": reason,
            "lang": (payment.metadata_json or {}).get("lang", "ru"),
            "chat_id": payment.chat_id,
            "message_id": payment.message_id,
        },
    )


def apply_payment(payment: Payment) -> None:

    if payment.status == "succeeded":
        return

    claim = db.session.execute(
        update(Payment).where(Payment.id == payment.id, Payment.status == "pending").values(status="processing")
    )
    db.session.commit()
    if claim.rowcount == 0:
        return

    tariff = db.session.get(Tariff, payment.tariff_id)

    rejected = False
    if tariff is None or not tariff.items:
        rejected = True
    else:
        try:
            _ensure_tariff_available(
                tariff,
                payment.telegram_id,
                where="billing.apply_payment",
                check_open_ended=False,
            )
        except ValueError:
            rejected = True

    if rejected:
        _fail_payment(payment, "tariff_unavailable")
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
            operation_id=f"pay:{payment.id}",
        )
    except LocalXrayUnavailable as exc:
        db.session.rollback()
        _fail_payment(payment, "provisioning_impossible")
        logger.error(
            "billing.apply_payment marked failed: payment=%s tariff=%s cannot be provisioned by this role: %s",
            payment.id,
            payment.tariff_id,
            exc,
        )
        return
    except Exception:
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
