from __future__ import annotations

import datetime as dt
import logging
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

import gevent
import yookassa
from sqlalchemy import case, or_, update
from sqlalchemy.orm import Session
from yookassa import Configuration

from panel_core.extensions import db
from panel_core.models import BotEvent, Payment, SystemSetting, Tariff, TelegramUser, UserTariffAccess
from panel_core.services import bot_events, provisioning, tariff_delivery
from panel_core.services.open_access import has_open_ended_access

logger = logging.getLogger(__name__)
_YK_CALL_TIMEOUT_S = 8
_LEASE_SECONDS = 120
_HEARTBEAT_SECONDS = 20


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _get_setting(key):
    row = SystemSetting.query.filter_by(key=key).first()
    return row.value if row and row.value else ""


def _configure_sdk():
    shop_id = _get_setting("yookassa_shop_id")
    secret = _get_setting("yookassa_secret_key")
    if not shop_id or not secret:
        raise ValueError("yookassa_not_configured")
    Configuration.account_id = shop_id
    Configuration.secret_key = secret


def _build_snapshot(tariff):
    return {
        "name": tariff.name,
        "price_rub": tariff.price_rub,
        "period_days": tariff.period_days,
        "visibility": tariff.visibility,
        "is_trial": tariff.is_trial,
        "items": [
            {"inbound_tag": item.inbound_tag, "traffic_gb": item.traffic_gb, "panel_id": item.panel_id}
            for item in tariff.items
        ],
    }


def _purchased_tariff(payment):
    snapshot = payment.tariff_snapshot
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("name"), str):
        raise ValueError("purchased_terms_missing")
    days, items = snapshot.get("period_days"), snapshot.get("items")
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0 or not isinstance(items, list) or not items:
        raise ValueError("purchased_terms_invalid")
    frozen = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("inbound_tag"), str) or not item["inbound_tag"]:
            raise ValueError("purchased_terms_invalid")
        if "panel_id" not in item or "traffic_gb" not in item:
            raise ValueError("purchased_terms_invalid")
        amount = item["traffic_gb"]
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
            raise ValueError("purchased_terms_invalid")
        frozen.append(SimpleNamespace(**item))
    return SimpleNamespace(**{**snapshot, "id": payment.tariff_id, "items": tuple(frozen)})


def _ensure_tariff_available(tariff, telegram_id, *, where="billing", check_open_ended=True):
    user = db.session.get(TelegramUser, telegram_id)
    if user is not None and user.blocked:
        raise ValueError("account_blocked")
    if tariff is None:
        raise ValueError("tariff_not_available")
    if check_open_ended and has_open_ended_access(telegram_id):
        raise ValueError("open_ended_access")
    if not tariff.enabled or tariff.visibility == "archived" or tariff.is_trial:
        raise ValueError("tariff_not_available")
    if (
        tariff.visibility == "private"
        and UserTariffAccess.query.filter_by(telegram_id=telegram_id, tariff_id=tariff.id).first() is None
    ):
        raise ValueError("tariff_not_available")
    if not tariff_delivery.is_deliverable(tariff):
        tariff_delivery.log_undeliverable(tariff, where)
        raise ValueError("tariff_not_available")


def payment_state(payment):
    return {
        "provider_status": payment.provider_status,
        "fulfillment_status": payment.fulfillment_status,
        "refund_status": payment.refund_status,
        "refunded_amount_kopeks": payment.refunded_amount_kopeks,
        "fulfillment_error": payment.fulfillment_error,
    }


def _checkout_result(payment):
    return {
        "payment_id": payment.id,
        "yookassa_id": payment.yookassa_id,
        "confirmation_url": payment.confirmation_url,
        "amount_rub": payment.amount_rub,
        **payment_state(payment),
    }


def create_checkout(*, telegram_id, tariff_id, lang):
    user = db.session.get(TelegramUser, telegram_id)
    if user is not None and user.blocked:
        raise ValueError("account_blocked")
    prior = (
        Payment.query.filter(
            Payment.telegram_id == telegram_id,
            Payment.tariff_id == tariff_id,
            or_(
                Payment.checkout_status == "creating",
                (Payment.checkout_status == "ready")
                & Payment.provider_idempotency_key.isnot(None)
                & Payment.provider_status.in_(("pending", "waiting_for_capture"))
                & Payment.cancel_requested_at.is_(None),
            ),
        )
        .order_by(Payment.id.asc())
        .first()
    )
    if prior is not None:
        return resume_checkout(prior)
    if (
        Payment.query.filter_by(telegram_id=telegram_id, tariff_id=tariff_id, checkout_status="review").first()
        is not None
    ):
        raise ValueError("checkout_requires_review")
    tariff = db.session.get(Tariff, tariff_id)
    _ensure_tariff_available(tariff, telegram_id, where="billing.create_checkout")
    _configure_sdk()
    payment = Payment(
        yookassa_id=f"pending-{uuid.uuid4().hex}",
        telegram_id=telegram_id,
        tariff_id=tariff.id,
        tariff_snapshot=_build_snapshot(tariff),
        amount_rub=tariff.price_rub,
        status="pending",
        checkout_status="creating",
        provider_idempotency_key=uuid.uuid4().hex,
        metadata_json={"telegram_id": telegram_id, "tariff_id": tariff.id, "lang": lang},
    )
    db.session.add(payment)
    db.session.flush()
    payment.checkout_payload = {
        "amount": {"value": f"{payment.amount_rub:.2f}", "currency": "RUB"},
        "description": f"{tariff.id}-{telegram_id}",
        "confirmation": {"type": "redirect", "return_url": _get_setting("yookassa_return_url") or "https://t.me/"},
        "capture": True,
        "metadata": {"payment_db_id": payment.id, "telegram_id": telegram_id, "tariff_id": tariff.id},
    }
    db.session.commit()
    return resume_checkout(payment)


def resume_checkout(payment):
    if payment.checkout_status == "ready" and not payment.yookassa_id.startswith("pending-"):
        return _checkout_result(payment)
    if not payment.provider_idempotency_key or not isinstance(payment.checkout_payload, dict):
        payment.checkout_status = "review"
        payment.fulfillment_error = "checkout_request_missing"
        db.session.commit()
        raise ValueError("checkout_requires_review")
    if payment.checkout_started_at is not None and _now() - payment.checkout_started_at >= dt.timedelta(
        hours=23, minutes=55
    ):
        payment.checkout_status = "review"
        payment.fulfillment_error = "checkout_idempotency_expired"
        db.session.commit()
        raise ValueError("checkout_requires_review")
    _configure_sdk()
    if payment.checkout_started_at is None:
        db.session.execute(
            update(Payment)
            .where(Payment.id == payment.id, Payment.checkout_started_at.is_(None))
            .values(checkout_started_at=_now())
        )
        db.session.commit()
    for attempt in range(2):
        try:
            remote = gevent.with_timeout(
                _YK_CALL_TIMEOUT_S, yookassa.Payment.create, payment.checkout_payload, payment.provider_idempotency_key
            )
            _bind_remote(payment, remote)
            return _checkout_result(payment)
        except gevent.Timeout as exc:
            if attempt:
                raise RuntimeError("yookassa_timeout") from exc
    raise RuntimeError("yookassa_unavailable")


def _kopeks(amount):
    try:
        value = Decimal(str(getattr(amount, "value", "0")))
        if not value.is_finite() or value < 0 or value * 100 != (value * 100).to_integral_value():
            raise ValueError("invalid_provider_amount")
        return int(value * 100)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid_provider_amount") from exc


def _validate_remote(payment, remote, *, metadata_required=False):
    if not getattr(remote, "id", None):
        raise ValueError("provider_payment_id_missing")
    if not payment.yookassa_id.startswith("pending-") and remote.id != payment.yookassa_id:
        raise ValueError("provider_payment_id_mismatch")
    amount = getattr(remote, "amount", None)
    if amount is None or getattr(amount, "currency", None) != "RUB" or _kopeks(amount) != payment.amount_rub * 100:
        raise ValueError("provider_payment_amount_mismatch")
    metadata = getattr(remote, "metadata", None) or {}
    expected = {"payment_db_id": payment.id, "telegram_id": payment.telegram_id, "tariff_id": payment.tariff_id}
    if metadata_required or metadata:
        if not isinstance(metadata, dict) or any(
            str(metadata.get(key)) != str(value) for key, value in expected.items()
        ):
            raise ValueError("provider_payment_metadata_mismatch")


def _bind_remote(payment, remote):
    _validate_remote(payment, remote, metadata_required=True)
    url = getattr(getattr(remote, "confirmation", None), "confirmation_url", None)
    payment.yookassa_id = remote.id
    payment.confirmation_url = url or payment.confirmation_url
    payment.checkout_status = "ready"
    if (payment.fulfillment_error or "").startswith("checkout_"):
        payment.fulfillment_error = None
    db.session.commit()


def fetch_remote_payment(yookassa_id):
    try:
        _configure_sdk()
        return gevent.with_timeout(_YK_CALL_TIMEOUT_S, yookassa.Payment.find_one, yookassa_id)
    except (Exception, gevent.Timeout):
        logger.warning("YooKassa lookup failed for %s", yookassa_id, exc_info=True)
        return None


def resolve_remote_payment(yookassa_id):
    remote = fetch_remote_payment(yookassa_id)
    if remote is None or getattr(remote, "id", None) != yookassa_id:
        return None, None
    payment = Payment.query.filter_by(yookassa_id=yookassa_id).first()
    if payment is None:
        metadata = getattr(remote, "metadata", None) or {}
        try:
            payment_id = int(metadata.get("payment_db_id"))
        except (TypeError, ValueError, AttributeError):
            return None, remote
        payment = db.session.get(Payment, payment_id)
        if payment is None or not payment.yookassa_id.startswith("pending-"):
            return None, remote
        _bind_remote(payment, remote)
    _record_remote_refund(payment, remote)
    return payment, remote


def fetch_remote_status(payment):
    remote = fetch_remote_payment(payment.yookassa_id)
    if remote is None:
        return None
    _record_remote_refund(payment, remote)
    return remote.status


def _record_remote_refund(payment, remote):
    _validate_remote(payment, remote)
    amount = getattr(remote, "refunded_amount", None)
    if remote.status != "succeeded" or amount is None:
        return
    if getattr(amount, "currency", None) != "RUB":
        raise ValueError("provider_refund_currency_mismatch")
    refunded = _kopeks(amount)
    db.session.execute(
        update(Payment)
        .where(Payment.id == payment.id, Payment.refunded_amount_kopeks <= refunded)
        .values(refunded_amount_kopeks=refunded, provider_status="succeeded")
    )
    db.session.commit()


def _event(payment, kind, extra=None):
    payload = {
        "payment_id": payment.id,
        "tariff_id": payment.tariff_id,
        "lang": (payment.metadata_json or {}).get("lang", "ru"),
        "chat_id": payment.chat_id,
        "message_id": payment.message_id,
        **(extra or {}),
    }
    event = BotEvent(type=kind, telegram_id=payment.telegram_id, payload=payload)
    db.session.add(event)
    return event


def _publish_committed(event):
    try:
        bot_events.publish_stored(event)
    except Exception:
        db.session.rollback()
        logger.exception("Committed payment event awaits replay")


def accept_remote_status(payment, status):
    if status == "succeeded":
        db.session.execute(
            update(Payment)
            .where(Payment.id == payment.id)
            .values(provider_status="succeeded", paid_at=payment.paid_at or _now())
        )
        db.session.commit()
        apply_payment(payment)
    elif status == "canceled":
        result = db.session.execute(
            update(Payment)
            .where(
                Payment.id == payment.id,
                Payment.provider_status != "succeeded",
                Payment.status.notin_(("succeeded", "refunded", "cancelled")),
            )
            .values(provider_status="canceled", status="cancelled")
        )
        event = _event(payment, "payment_cancelled") if result.rowcount else None
        db.session.commit()
        if event is not None:
            _publish_committed(event)
    elif status in ("pending", "waiting_for_capture"):
        db.session.execute(
            update(Payment)
            .where(Payment.id == payment.id, Payment.provider_status.notin_(("succeeded", "canceled")))
            .values(provider_status=status)
        )
        db.session.commit()


class PaymentLeaseLost(RuntimeError):
    pass


class PaymentBlocked(RuntimeError):
    pass


def _claim(payment, kind):
    owner = str(uuid.uuid4())
    state = Payment.fulfillment_status if kind == "fulfillment" else Payment.refund_status
    terminal = ("succeeded",) if kind == "fulfillment" else ("completed",)
    values = {
        "processing_owner": owner,
        "processing_version": Payment.processing_version + 1,
        "processing_expires_at": _now() + dt.timedelta(seconds=_LEASE_SECONDS),
        f"{kind}_status": "processing",
    }
    if kind == "fulfillment":
        values["status"] = "processing"
    result = db.session.execute(
        update(Payment)
        .where(Payment.id == payment.id, Payment.processing_owner.is_(None), state.notin_(terminal))
        .values(**values)
    )
    db.session.commit()
    if not result.rowcount:
        return None
    db.session.refresh(payment)
    return owner, payment.processing_version


def _lease_where(payment_id, claim):
    owner, version = claim
    return (
        Payment.id == payment_id,
        Payment.processing_owner == owner,
        Payment.processing_version == version,
        Payment.processing_expires_at > _now(),
    )


@contextmanager
def _guarded_lease(payment, claim, *, check_block):
    engine, payment_id, telegram_id = db.engine, payment.id, payment.telegram_id

    def guard():
        with Session(engine) as session:
            row = session.query(Payment).filter(*_lease_where(payment_id, claim)).first()
            if row is None:
                raise PaymentLeaseLost("payment_processing_owner_changed")
            if check_block:
                user = session.get(TelegramUser, telegram_id)
                if user is not None and user.blocked:
                    raise PaymentBlocked("account_blocked")
                if row.refunded_amount_kopeks >= row.amount_rub * 100:
                    raise PaymentBlocked("payment_fully_refunded")

    def heartbeat():
        while True:
            gevent.sleep(_HEARTBEAT_SECONDS)
            try:
                with Session(engine) as session:
                    changed = session.execute(
                        update(Payment)
                        .where(*_lease_where(payment_id, claim))
                        .values(processing_expires_at=_now() + dt.timedelta(seconds=_LEASE_SECONDS))
                    )
                    session.commit()
                    if not changed.rowcount:
                        return
            except Exception:
                logger.exception("Payment heartbeat failed payment=%s", payment_id)
                return

    task = gevent.spawn(heartbeat)
    try:
        guard()
        yield guard
    finally:
        task.kill(block=False)


def _finish(payment, claim, values, *, event=None, payload=None):
    result = db.session.execute(
        update(Payment)
        .where(*_lease_where(payment.id, claim))
        .values(**values, processing_owner=None, processing_expires_at=None)
    )
    stored = _event(payment, event, payload) if result.rowcount and event else None
    db.session.commit()
    if stored is not None:
        _publish_committed(stored)
    return bool(result.rowcount)


def apply_payment(payment):
    db.session.refresh(payment)
    if payment.fulfillment_status == "succeeded" or payment.status in ("succeeded", "refunded"):
        return
    payment.provider_status = "succeeded"
    payment.paid_at = payment.paid_at or _now()
    db.session.commit()
    claim = _claim(payment, "fulfillment")
    if claim is None:
        return
    try:
        tariff = _purchased_tariff(payment)
        with _guarded_lease(payment, claim, check_block=True) as guard:
            result = provisioning.apply_tariff_for_user(
                telegram_id=payment.telegram_id,
                tariff=tariff,
                source="yookassa",
                operation_id=f"pay:{payment.id}",
                guard=guard,
            )
            guard()
            _finish(
                payment,
                claim,
                {"status": "succeeded", "fulfillment_status": "succeeded", "fulfillment_error": None},
                event="payment_succeeded",
                payload={
                    "tariff_name": tariff.name,
                    "amount_rub": payment.amount_rub,
                    "expires_at_ms": result["expires_at_ms"],
                },
            )
    except PaymentLeaseLost:
        db.session.rollback()
        logger.warning("Payment processing lease lost payment=%s", payment.id)
    except PaymentBlocked as exc:
        db.session.rollback()
        _finish(payment, claim, {"status": "pending", "fulfillment_status": "blocked", "fulfillment_error": str(exc)})
    except ValueError as exc:
        db.session.rollback()
        _finish(payment, claim, {"status": "pending", "fulfillment_status": "review", "fulfillment_error": str(exc)})
    except Exception as exc:
        db.session.rollback()
        _finish(
            payment, claim, {"status": "pending", "fulfillment_status": "retry", "fulfillment_error": str(exc)[:1000]}
        )
        raise


def handle_refund(payment):
    db.session.refresh(payment)
    if payment.refund_status == "completed":
        return
    if payment.provider_status != "succeeded" and payment.status not in ("succeeded", "refunded"):
        return
    if payment.refunded_amount_kopeks < payment.amount_rub * 100:
        remote = fetch_remote_payment(payment.yookassa_id)
        if remote is None:
            return
        _validate_remote(payment, remote)
        if remote.status != "succeeded":
            return
        amount = getattr(remote, "refunded_amount", None)
        if amount is None:
            return
        if getattr(amount, "currency", None) != "RUB":
            raise ValueError("provider_refund_currency_mismatch")
        refunded = _kopeks(amount)
        db.session.execute(
            update(Payment)
            .where(Payment.id == payment.id, Payment.refunded_amount_kopeks <= refunded)
            .values(refunded_amount_kopeks=refunded, provider_status="succeeded")
        )
        db.session.commit()
        db.session.refresh(payment)
    if not payment.refunded_amount_kopeks:
        return
    if payment.refunded_amount_kopeks < payment.amount_rub * 100:
        db.session.execute(
            update(Payment)
            .where(
                Payment.id == payment.id,
                Payment.refunded_amount_kopeks < Payment.amount_rub * 100,
                Payment.refund_status.notin_(("processing", "completed")),
            )
            .values(
                refund_status="partial", status=case((Payment.status == "refunded", "succeeded"), else_=Payment.status)
            )
        )
        db.session.commit()
        return
    claim = _claim(payment, "refund")
    if claim is None:
        return
    try:
        with _guarded_lease(payment, claim, check_block=False) as guard:
            result = provisioning.revoke_payment_access(
                payment.telegram_id,
                payment.tariff_id,
                operation_id=f"pay:{payment.id}",
                tariff_snapshot=payment.tariff_snapshot,
                pending_targets=payment.refund_pending_targets,
                guard=guard,
            )
            guard()
            failures = result.get("panel_failures", [])
            _finish(
                payment,
                claim,
                {
                    "status": "refunded",
                    "refund_status": "pending" if failures else "completed",
                    "refund_pending_targets": failures,
                    "fulfillment_error": "refund_delivery_pending" if failures else None,
                },
                event=None if failures else "payment_refunded",
            )
    except PaymentLeaseLost:
        db.session.rollback()
    except Exception as exc:
        db.session.rollback()
        _finish(payment, claim, {"refund_status": "pending", "fulfillment_error": str(exc)[:1000]})
        raise
