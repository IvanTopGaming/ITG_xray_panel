import hmac
import hashlib
import logging
import secrets
import time

from flask import Blueprint, request, jsonify

from panel_core.extensions import db, limiter
from panel_core.models import FederationConfig, Inbound
from panel_core.utils import federation_token_required, token_required
from panel_core.services.reality_health import read_failures
from panel_core.version import get_app_version

logger = logging.getLogger(__name__)

bp = Blueprint("federation", __name__)


def _build_panel_url() -> str:
    import os

    domain = os.environ.get("PANEL_DOMAIN", "").strip()
    secret = os.environ.get("PANEL_SECRET_PATH", "").strip()
    if not domain:
        domain = request.host
    scheme = "https"
    url = f"{scheme}://{domain}"
    if secret:
        url += f"/{secret}"
    return url


@bp.route("/federation/link-token", methods=["POST"])
@token_required
def generate_link_token():

    cfg = db.session.get(FederationConfig, 1)
    if cfg is None:
        cfg = FederationConfig(id=1)
        db.session.add(cfg)

    revoked = bool(cfg.federation_token)

    raw_token = secrets.token_urlsafe(32)
    cfg.link_token = raw_token
    cfg.link_token_used = False
    cfg.link_request_id = None
    cfg.federation_token = None
    cfg.linked_at = None
    db.session.commit()

    if revoked:
        logger.info(
            "federation access revoked: a fresh link token was issued while linked to %s",
            cfg.master_url or "an unnamed master",
        )

    panel_url = _build_panel_url()
    import base64

    composite = base64.urlsafe_b64encode(f"{panel_url}|{raw_token}".encode()).decode().rstrip("=")

    return jsonify({"link_token": composite, "revoked": revoked}), 200


@bp.route("/federation/handshake", methods=["POST"])
@limiter.limit("30 per minute")
def handshake():

    data = request.get_json(silent=True) or {}
    incoming_token = str(data.get("link_token") or "")
    master_url = str(data.get("master_url") or "").strip()
    master_name = str(data.get("master_name") or "").strip()
    request_id = str(data.get("request_id") or "").strip()
    if len(request_id) > 64:
        return jsonify({"error": "invalid request_id"}), 400

    if not incoming_token:
        return jsonify({"error": "link_token is required"}), 401

    import base64

    try:
        decoded = base64.urlsafe_b64decode(incoming_token + "==").decode()
        if "|" in decoded:
            incoming_token = decoded.split("|", 1)[1]
    except Exception:
        pass

    cfg = db.session.get(FederationConfig, 1)
    if cfg is None or not cfg.link_token:
        return jsonify({"error": "no pending link token"}), 401

    if not hmac.compare_digest(incoming_token.encode(), cfg.link_token.encode()):
        return jsonify({"error": "invalid link token"}), 401

    request_id = request_id or hashlib.sha256(f"{master_url}|{incoming_token}".encode()).hexdigest()
    if cfg.link_token_used:
        if cfg.link_request_id != request_id or (cfg.master_url or "") != master_url:
            return jsonify({"error": "link token already used"}), 401
        return _handshake_reply(cfg.federation_token)

    federation_token = secrets.token_urlsafe(32)

    claimed = FederationConfig.query.filter(
        FederationConfig.id == 1,
        FederationConfig.link_token == incoming_token,
        FederationConfig.link_token_used.is_(False),
    ).update(
        {
            "federation_token": federation_token,
            "master_url": master_url or None,
            "master_name": master_name or None,
            "link_token_used": True,
            "link_request_id": request_id,
            "linked_at": int(time.time() * 1000),
        },
        synchronize_session=False,
    )
    db.session.commit()
    if claimed != 1:
        db.session.refresh(cfg)
        if cfg.link_request_id != request_id or (cfg.master_url or "") != master_url:
            return jsonify({"error": "link token already used"}), 401
        federation_token = cfg.federation_token
    return _handshake_reply(federation_token)


def _handshake_reply(federation_token):
    from panel_core.services.node_identity import get_or_create_instance_id

    inbound_count = Inbound.query.count()

    return jsonify(
        {
            "federation_token": federation_token,
            "inbound_count": inbound_count,
            "instance_id": get_or_create_instance_id(),
        }
    ), 200


@bp.route("/federation/snapshot", methods=["GET"])
@limiter.exempt
@federation_token_required
def snapshot():
    from panel_core.services.state_export import export_state

    state = export_state(include_cold=False)

    return jsonify(
        {
            "app_version": get_app_version(),
            "status": "ok",
            "timestamp": state["timestamp"],
            "reality_failures": read_failures(),
            "inbounds": state["hot"]["inbounds"],
            "cold_fingerprint": state["fingerprint"],
            "instance_id": state["instance_id"],
        }
    ), 200


@bp.route("/federation/state", methods=["GET"])
@limiter.exempt
@federation_token_required
def full_state():
    from panel_core.services.state_export import export_state

    return jsonify({"app_version": get_app_version(), **export_state()}), 200


@bp.route("/federation/config", methods=["GET"])
@token_required
def get_config():

    cfg = db.session.get(FederationConfig, 1)
    if cfg is None:
        return jsonify(
            {
                "master_url": None,
                "master_name": None,
                "linked_at": None,
                "link_token": None,
                "is_linked": False,
            }
        ), 200

    link_token = None
    if cfg.link_token and not cfg.link_token_used:
        import base64

        panel_url = _build_panel_url()
        link_token = base64.urlsafe_b64encode(f"{panel_url}|{cfg.link_token}".encode()).decode().rstrip("=")

    return jsonify(
        {
            "master_url": cfg.master_url,
            "master_name": cfg.master_name,
            "linked_at": cfg.linked_at,
            "link_token": link_token,
            "is_linked": bool(cfg.federation_token and cfg.linked_at),
        }
    ), 200


@bp.route("/federation/provision", methods=["POST"])
@limiter.exempt
@federation_token_required
def provision():

    data = request.get_json(silent=True) or {}

    telegram_id = data.get("telegram_id")
    inbound_tag = data.get("inbound_tag")
    expiry_ms = data.get("expiry_ms")
    period_ms = data.get("period_ms")
    limit_bytes = data.get("limit_bytes")
    tariff_id = data.get("tariff_id")
    idempotency_key = data.get("idempotency_key")

    if telegram_id is None or inbound_tag is None:
        return jsonify({"error": "telegram_id and inbound_tag are required"}), 400

    from panel_core.services.provisioning import provision_single_item

    try:
        result = provision_single_item(
            telegram_id=telegram_id,
            inbound_tag=inbound_tag,
            expiry_ms=expiry_ms,
            period_ms=period_ms,
            limit_bytes=limit_bytes,
            tariff_id=tariff_id,
            idempotency_key=idempotency_key,
            source_id=data.get("source_id"),
            source_revision=data.get("source_revision", 0),
            operation_id=data.get("operation_id"),
            account_revision=data.get("account_revision", 0),
        )
        return jsonify(result), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("provision failed: %s", exc)
        return jsonify({"error": "internal server error"}), 500


@bp.route("/federation/entitlements/revoke", methods=["POST"])
@limiter.exempt
@federation_token_required
def revoke_entitlement():
    from panel_core.services.entitlements import revoke_source

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "expected_object"}), 400
    try:
        return jsonify(
            revoke_source(
                telegram_id=int(data["telegram_id"]),
                inbound_tag=data["inbound_tag"],
                source_id=data["source_id"],
                source_revision=int(data.get("source_revision", 1)),
                operation_id=data.get("operation_id"),
                tariff_id=data.get("tariff_id"),
                revoked_sources=data.get("revoked_sources"),
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400


@bp.route("/federation/entitlements/reset-cycle", methods=["POST"])
@federation_token_required
@limiter.exempt
def reset_entitlement_cycle():
    from panel_core.services.entitlements import reset_source_cycle

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected JSON object"}), 400
    try:
        return jsonify(
            reset_source_cycle(
                telegram_id=payload["telegram_id"],
                tariff_id=payload.get("tariff_id"),
                inbound_tag=payload["inbound_tag"],
                source_id=payload["source_id"],
                source_revision=payload["source_revision"],
                operation_id=payload["operation_id"],
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400


@bp.route("/federation/account-access", methods=["POST"])
@limiter.exempt
@federation_token_required
def account_access():
    from panel_core.services.entitlements import apply_account_state

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("blocked"), bool):
        return jsonify({"error": "invalid_account_state"}), 400
    try:
        return jsonify(
            apply_account_state(
                telegram_id=int(data["telegram_id"]), revision=int(data["revision"]), blocked=data["blocked"]
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400


@bp.route("/federation/events/<int:event_id>", methods=["GET"])
@limiter.exempt
@federation_token_required
def read_bot_event(event_id):
    from panel_core.models import BotEvent
    from panel_core.services.bot_delivery import _envelope

    row = BotEvent.query.filter_by(origin_event_id=event_id, source=request.args.get("source", "")).first()
    if row is None:
        return jsonify({"error": "event not found"}), 404
    return jsonify(_envelope(row))


@bp.route("/federation/events/<int:event_id>/ack", methods=["POST"])
@limiter.exempt
@federation_token_required
def acknowledge_bot_event(event_id):
    import datetime as dt
    from panel_core.models import BotEvent

    data = request.get_json(silent=True) or {}
    source = data.get("source")
    if not isinstance(source, str) or not source:
        return jsonify({"error": "source is required"}), 400
    matched = BotEvent.query.filter_by(origin_event_id=event_id, source=source).update(
        {"delivered_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)}, synchronize_session=False
    )
    db.session.commit()
    return jsonify({"acked": bool(matched)}), 200 if matched else 404
