"""Billing endpoints. Checkout: bot service token. Webhook: status re-verified against YooKassa."""

import logging

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.models import Payment
from app.services import billing, bot_events
from app.utils import bot_service_token_required

logger = logging.getLogger(__name__)
bp = Blueprint("billing", __name__)


@bp.route("/billing/checkout", methods=["POST"])
@bot_service_token_required
def checkout():
    payload = request.get_json(silent=True) or {}
    try:
        telegram_id = int(payload["telegram_id"])
        tariff_id = int(payload["tariff_id"])
        lang = str(payload.get("lang") or "ru")
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "invalid_request"}), 400

    try:
        result = billing.create_checkout(telegram_id=telegram_id, tariff_id=tariff_id, lang=lang)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        # Split SQLite-contention from YooKassa-unreachable so the bot can
        # show a retry hint instead of "tariff unavailable for renewal".
        from sqlalchemy.exc import OperationalError as _SAOperationalError

        if isinstance(exc, _SAOperationalError) and "database is locked" in str(exc.orig or ""):
            logger.warning("billing.checkout: db_locked tg=%s tariff=%s", telegram_id, tariff_id)
            return jsonify({"error": "db_busy"}), 503
        logger.exception("billing.checkout: yookassa error")
        return jsonify({"error": "yookassa_unavailable"}), 502
    return jsonify(result), 200


@bp.route("/billing/yookassa/webhook", methods=["POST"])
def yookassa_webhook():
    body = request.get_json(silent=True) or {}
    event = body.get("event")
    obj = body.get("object") or {}
    yk_id = obj.get("id")
    if not event or not yk_id:
        return jsonify({"error": "invalid_request"}), 400

    payment = Payment.query.filter_by(yookassa_id=yk_id).first()
    if payment is None:
        logger.info("yookassa_webhook: unknown payment yk=%s", yk_id)
        return jsonify({"ok": True}), 200

    # YooKassa webhooks carry no signature, so the body is only a trigger.
    # Re-fetch the authoritative status before acting — a forged notification
    # then provisions nothing. None means the lookup was unavailable; the poll
    # cron retries the row, so we simply do nothing here.
    real_status = billing.fetch_remote_status(payment)

    try:
        if real_status == "succeeded":
            logger.info("yookassa_webhook: payment succeeded id=%s yk=%s", payment.id, yk_id)
            billing.apply_payment(payment)
        elif real_status == "canceled":
            if payment.status not in ("succeeded", "cancelled"):
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
        logger.exception("yookassa_webhook: handler crashed yk=%s", yk_id)
    return jsonify({"ok": True}), 200
