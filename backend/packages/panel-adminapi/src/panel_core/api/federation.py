import hmac
import json
import logging
import secrets
import time

from flask import Blueprint, request, jsonify

from panel_core.extensions import db, limiter
from panel_core.models import FederationConfig, Inbound, SystemSetting
from panel_core.utils import federation_token_required, token_required

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

    if cfg.federation_token and cfg.linked_at:
        return jsonify({"error": "already linked to a master panel"}), 409

    raw_token = secrets.token_urlsafe(32)
    cfg.link_token = raw_token
    cfg.link_token_used = False
    db.session.commit()

    panel_url = _build_panel_url()
    import base64

    composite = base64.urlsafe_b64encode(f"{panel_url}|{raw_token}".encode()).decode().rstrip("=")

    return jsonify({"link_token": composite}), 200


@bp.route("/federation/handshake", methods=["POST"])
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

    name_setting = SystemSetting.query.filter_by(key="panel_name").first()
    panel_name = name_setting.value if name_setting and name_setting.value else "Panel"

    inbound_count = Inbound.query.count()

    return jsonify(
        {
            "federation_token": federation_token,
            "name": panel_name,
            "inbound_count": inbound_count,
        }
    ), 200


@bp.route("/federation/snapshot", methods=["GET"])
@limiter.exempt
@federation_token_required
def snapshot():

    name_setting = SystemSetting.query.filter_by(key="panel_name").first()
    panel_name = name_setting.value if name_setting and name_setting.value else "Panel"

    inbounds = Inbound.query.all()
    result_inbounds = []

    for ib in inbounds:
        ss = ib.stream_settings
        if isinstance(ss, str):
            try:
                ss = json.loads(ss)
            except (json.JSONDecodeError, TypeError):
                ss = {}
        if ss is None:
            ss = {}

        clients_data = []
        for c in ib.clients:
            device_count = len(c.devices) if c.devices else 0
            clients_data.append(
                {
                    "id": c.id,
                    "email": c.email,
                    "enable": bool(c.enable),
                    "up": c.up or 0,
                    "down": c.down or 0,
                    "limit_bytes": c.limit_bytes or 0,
                    "expiry_time": c.expiry_time or 0,
                    "reset_day": c.reset_day or 0,
                    "flow": c.flow or "",
                    "last_seen": c.last_seen if c.last_seen else None,
                    "device_count": device_count,
                    "tariff_id": c.tariff_id,
                    "telegram_id": c.telegram_id,
                }
            )

        result_inbounds.append(
            {
                "tag": ib.tag,
                "port": ib.port,
                "protocol": ib.protocol,
                "label": ib.label or "",
                "stream_settings": ss,
                "up": ib.up or 0,
                "down": ib.down or 0,
                "fallback_address": ib.fallback_address or "",
                "device_limit": ib.device_limit or 0,
                "routing_profile_id": ib.routing_profile_id,
                "clients": clients_data,
            }
        )

    return jsonify(
        {
            "panel_name": panel_name,
            "status": "ok",
            "timestamp": int(time.time() * 1000),
            "inbounds": result_inbounds,
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

    try:
        from panel_core.services.provisioning import provision_single_item
    except ImportError:
        return jsonify({"error": "provision_single_item not implemented yet"}), 501

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
