import hmac
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

    if cfg.link_token_used:
        return jsonify({"error": "link token already used"}), 401

    if not hmac.compare_digest(incoming_token, cfg.link_token):
        return jsonify({"error": "invalid link token"}), 401

    federation_token = secrets.token_urlsafe(32)

    cfg.federation_token = federation_token
    cfg.master_url = master_url or None
    cfg.master_name = master_name or None
    cfg.link_token_used = True
    cfg.linked_at = int(time.time() * 1000)
    db.session.commit()

    inbound_count = Inbound.query.count()

    return jsonify(
        {
            "federation_token": federation_token,
            "inbound_count": inbound_count,
        }
    ), 200


@bp.route("/federation/snapshot", methods=["GET"])
@limiter.exempt
@federation_token_required
def snapshot():
    from panel_core.services.node_identity import get_or_create_instance_id
    from panel_core.services.state_export import export_hot_state
    from panel_core.services.state_fingerprint import compute_fingerprint

    hot = export_hot_state()

    return jsonify(
        {
            "app_version": get_app_version(),
            "status": "ok",
            "timestamp": int(time.time() * 1000),
            "reality_failures": read_failures(),
            "inbounds": hot["inbounds"],
            "cold_fingerprint": compute_fingerprint(),
            "instance_id": get_or_create_instance_id(),
        }
    ), 200


@bp.route("/federation/state", methods=["GET"])
@limiter.exempt
@federation_token_required
def full_state():
    from panel_core.services.node_identity import get_or_create_instance_id
    from panel_core.services.state_export import export_cold_state, export_hot_state
    from panel_core.services.state_fingerprint import compute_fingerprint

    return jsonify(
        {
            "app_version": get_app_version(),
            "hot": export_hot_state(),
            "cold": export_cold_state(),
            "fingerprint": compute_fingerprint(),
            "instance_id": get_or_create_instance_id(),
        }
    ), 200


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
        )
        return jsonify(result), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("provision failed: %s", exc)
        return jsonify({"error": "internal server error"}), 500
