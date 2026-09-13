from flask import Blueprint, current_app, jsonify, request

from panel_core.extensions import db
from panel_core.services import bot_delivery
from panel_core.utils import bot_service_token_required


bp = Blueprint("bot_delivery", __name__)


@bp.route("/bot-service/events/claim", methods=["POST"])
@bot_service_token_required
def claim():
    try:
        result = bot_delivery.claim_event(request.get_json(silent=True))
    except (ValueError, TypeError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning("event claim unavailable: %s", type(exc).__name__)
        return jsonify({"error": "event_delivery_unavailable"}), 503
    return jsonify(result)


@bp.route("/bot-service/events/ack", methods=["POST"])
@bot_service_token_required
def ack():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "body must be an object"}), 400
    try:
        accepted = bot_delivery.ack_event(
            data.get("source"), data.get("id"), data.get("lease_token"), data.get("outcome")
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"acked": accepted}), 200 if accepted else 409


@bp.route("/bot-service/events/pending", methods=["GET"])
@bot_service_token_required
def pending():
    return jsonify({"events": bot_delivery.pending_events()})
