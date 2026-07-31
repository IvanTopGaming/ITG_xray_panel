import psutil
import hmac
import os
import json
from flask import Blueprint, jsonify, request, Response, stream_with_context
from panel_core.utils import token_required, admin_or_federation_token_required
from panel_core.extensions import limiter, db
from panel_core.models import SystemSetting
from panel_core.services.egress import build_bind_ips
from panel_core.xray.facade import (
    has_local_xray,
    restart_xray_container,
    update_geo_db,
    stream_xray_logs,
    generate_config_file,
)
from panel_core.xray.gateway import LocalXrayUnavailable
from panel_core.xray.settings import get_system_settings
from panel_core.xray.protocol import (
    normalize_geo_data_url,
    normalize_xray_log_level,
)
from panel_core.version import get_app_version, app_version_key
from panel_core.services.bot_status import get_bot_status
from panel_core.services.version_check import get_latest

bp = Blueprint("system", __name__)
XRAY_LOGS_UNSUPPORTED = "Xray logs are served by the node that runs Xray; this role has no local Xray instance."
XRAY_RESTART_UNSUPPORTED = "Restarting Xray is done on the node that runs it; this role has no local Xray instance."
XRAY_GEO_UNSUPPORTED = "Geo databases live on the node that runs Xray; this role has no local Xray instance."
XRAY_SETTINGS_UNSUPPORTED = (
    "Xray settings (log level, GeoIP/GeoSite URLs) are read by the node that generates the Xray "
    "config; this role has no local Xray instance, so a value stored here would reach nothing."
)
XRAY_CONFIG_UNSUPPORTED = "The Xray config file lives on the node that runs Xray; this role has no local Xray instance."


def _set_system_setting(key, value):
    item = SystemSetting.query.filter_by(key=key).first()
    if item:
        item.value = str(value)
    else:
        db.session.add(SystemSetting(key=key, value=str(value)))


@bp.route("/stats/system", methods=["GET"])
@admin_or_federation_token_required
@limiter.limit("60 per minute")
def get_system_stats():
    try:
        mem = psutil.virtual_memory()
        return jsonify(
            {
                "cpu": psutil.cpu_percent(interval=None),
                "mem_used": round(mem.used / (1024**3), 1),
                "mem_total": round(mem.total / (1024**3), 1),
                "mem_percent": mem.percent,
            }
        )
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/restart", methods=["POST"])
@admin_or_federation_token_required
@limiter.limit("5 per minute")
def restart():
    if not has_local_xray():
        return jsonify({"error": XRAY_RESTART_UNSUPPORTED}), 501
    try:
        restart_xray_container()
        return jsonify({"status": "restarted"}), 200
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/system/version", methods=["GET"])
@token_required
def system_version():
    bot = get_bot_status()
    latest = get_latest()
    return jsonify(
        {
            "running": {
                "backend": get_app_version(),
                "backend_key": app_version_key(),
                "bot": bot["version"],
                "bot_reported_at": bot["reported_at"],
            },
            "latest": latest["latest"],
            "latest_checked_at": latest["checked_at"],
        }
    )


@bp.route("/logs", methods=["GET"])
@token_required
def get_logs():
    if not has_local_xray():
        return jsonify({"error": XRAY_LOGS_UNSUPPORTED}), 501
    try:
        lines = stream_xray_logs()
    except LocalXrayUnavailable:
        return jsonify({"error": XRAY_LOGS_UNSUPPORTED}), 501

    def generate():
        for line in lines:
            clean_line = str(line).rstrip("\r\n")
            yield f"data: {clean_line}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@bp.route("/system/settings", methods=["GET"])
@token_required
@limiter.limit("60 per minute")
def system_settings_get():
    if not has_local_xray():
        return jsonify({"error": XRAY_SETTINGS_UNSUPPORTED}), 501
    try:
        return jsonify(get_system_settings())
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/system/settings", methods=["PUT"])
@token_required
@limiter.limit("20 per minute")
def system_settings_update():
    if not has_local_xray():
        return jsonify({"error": XRAY_SETTINGS_UNSUPPORTED}), 501
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            raise ValueError("Invalid request payload")

        updates = {}
        if "xrayLogLevel" in data:
            updates["xray_log_level"] = normalize_xray_log_level(data.get("xrayLogLevel"))
        if "geoipUrl" in data:
            updates["geoip_url"] = normalize_geo_data_url(data.get("geoipUrl"), "GeoIP URL")
        if "geositeUrl" in data:
            updates["geosite_url"] = normalize_geo_data_url(data.get("geositeUrl"), "GeoSite URL")

        if not updates:
            raise ValueError("No settings provided")

        current_settings = get_system_settings()
        should_restart = "xray_log_level" in updates and updates["xray_log_level"] != current_settings["xrayLogLevel"]

        for key, value in updates.items():
            _set_system_setting(key, value)
        db.session.commit()

        if should_restart:
            generate_config_file()
            restart_xray_container()

        return jsonify(get_system_settings()), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/server-keys", methods=["POST"])
@token_required
@limiter.limit("30 per minute")
def keys():
    from panel_core.xray.protocol import (
        generate_proxy_credentials,
        generate_reality_keys,
        generate_reality_short_id,
        generate_shadowsocks_password,
        generate_wireguard_keys,
    )

    try:
        payload = request.get_json(silent=True) or {}
        key_type = str(payload.get("type", "reality")).strip().lower()

        if key_type == "reality":
            payload = generate_reality_keys()
            payload["shortId"] = generate_reality_short_id()
            return jsonify(payload)
        if key_type in {"short-id", "shortid", "reality-short-id"}:
            return jsonify({"shortId": generate_reality_short_id()})
        if key_type in {"proxy-auth", "proxyauth", "auth", "credentials"}:
            return jsonify(generate_proxy_credentials())
        if key_type in {"password", "shadowsocks-password", "ss-password"}:
            method = str(payload.get("method", "") or "").strip()
            return jsonify({"password": generate_shadowsocks_password(method)})
        if key_type == "wireguard":
            return jsonify(generate_wireguard_keys())
        return jsonify({"error": "Unsupported key type"}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/config", methods=["GET"])
@token_required
@limiter.limit("60 per minute")
def get_config():
    if not has_local_xray():
        return jsonify({"error": XRAY_CONFIG_UNSUPPORTED}), 501
    try:
        config_path = "/etc/xray/config.json"
        if not os.path.exists(config_path):
            return jsonify({"error": "Config file not found"}), 404

        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data)
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/system/update-geo", methods=["POST"])
@token_required
@limiter.limit("10 per hour")
def geo_update():
    if not has_local_xray():
        return jsonify({"error": XRAY_GEO_UNSUPPORTED}), 501
    try:
        update_geo_db()
        return jsonify({"status": "updated"}), 200
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/system/egress/bind-ips", methods=["GET"])
def egress_bind_ips():
    expected = os.environ.get("EGRESS_INTERNAL_TOKEN", "")
    if not expected:
        return jsonify({"error": "egress token not configured"}), 503
    provided = request.headers.get("X-Egress-Token", "")
    if not hmac.compare_digest(provided, expected):
        return jsonify({"error": "forbidden"}), 403
    return jsonify(build_bind_ips())
