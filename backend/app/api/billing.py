"""Billing endpoints. Checkout: bot service token. Webhook: unauthenticated, IP-whitelisted."""

import ipaddress
import logging

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.models import Payment
from app.services import billing, bot_events
from app.utils import bot_service_token_required

logger = logging.getLogger(__name__)
bp = Blueprint("billing", __name__)

_YOOKASSA_NETS = [
    ipaddress.ip_network("185.71.76.0/27"),
    ipaddress.ip_network("185.71.77.0/27"),
    ipaddress.ip_network("77.75.153.0/25"),
    ipaddress.ip_network("77.75.156.11/32"),
    ipaddress.ip_network("77.75.156.35/32"),
    ipaddress.ip_network("77.75.154.128/25"),
    ipaddress.ip_network("2a02:5180::/32"),
]


def _client_ip() -> str:
    # Leftmost XFF is attacker-controlled; Caddy appends the real IP rightmost.
    raw_xff = (request.headers.get("X-Forwarded-For") or "").strip()
    if raw_xff:
        return raw_xff.rsplit(",", 1)[-1].strip()
    return request.remote_addr or ""


def _is_yookassa_ip(raw_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    return any(ip in net for net in _YOOKASSA_NETS)


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
    if not _is_yookassa_ip(_client_ip()):
        logger.warning("yookassa_webhook: rejected IP %s", _client_ip())
        return jsonify({"error": "forbidden"}), 403

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

    try:
        if event == "payment.succeeded":
            billing.apply_payment(payment)
        elif event == "payment.canceled":
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
