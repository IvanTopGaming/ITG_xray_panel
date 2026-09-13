import logging

from flask import Blueprint, jsonify, request

from panel_core.extensions import db, limiter
from panel_core.services import billing
from panel_core.utils import bot_service_token_required

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
        from sqlalchemy.exc import OperationalError as _SAOperationalError

        if isinstance(exc, _SAOperationalError) and "database is locked" in str(exc.orig or ""):
            logger.warning("billing.checkout: db_locked tg=%s tariff=%s", telegram_id, tariff_id)
            return jsonify({"error": "db_busy"}), 503
        logger.exception("billing.checkout: yookassa error")
        return jsonify({"error": "yookassa_unavailable"}), 502
    return jsonify(result), 200


@bp.route("/billing/yookassa/webhook", methods=["POST"])
@limiter.limit("60 per minute")
def yookassa_webhook():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "invalid_request"}), 400
    event, obj = body.get("event"), body.get("object")
    if not isinstance(event, str) or not isinstance(obj, dict):
        return jsonify({"error": "invalid_request"}), 400
    yk_id = obj.get("payment_id") if event.startswith("refund.") else obj.get("id")
    if not isinstance(yk_id, str) or not yk_id:
        return jsonify({"error": "invalid_request"}), 400
    try:
        payment, remote = billing.resolve_remote_payment(yk_id)
        if remote is None:
            return jsonify({"error": "provider_unavailable"}), 503
        if payment is None:
            return jsonify({"ok": True}), 200
        if event.startswith("refund."):
            billing.handle_refund(payment)
        else:
            billing.accept_remote_status(payment, remote.status)
    except ValueError:
        db.session.rollback()
        logger.warning("yookassa_webhook: rejected provider binding yk=%s", yk_id, exc_info=True)
        return jsonify({"error": "payment_mismatch"}), 400
    except Exception:
        db.session.rollback()
        logger.exception("yookassa_webhook: handler crashed yk=%s", yk_id)
        return jsonify({"error": "processing_unavailable"}), 503
    return jsonify({"ok": True}), 200
