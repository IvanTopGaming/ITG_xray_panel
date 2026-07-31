import os
import time

import requests
from flask import Blueprint, request, jsonify

from panel_core.extensions import db
from panel_core.models import LinkedPanel, SystemSetting, TariffItem
from panel_core.services.panel_proxy import _nudge_panel_refresh, forget_panel, get_panel_liveness
from panel_core.services.tariffs import purge_tariff_items
from panel_core.utils import token_required

bp = Blueprint("panels", __name__)


class _HandshakeError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _handshake(url: str, link_token: str) -> str:

    setting = SystemSetting.query.filter_by(key="panel_name").first()
    master_name = (setting.value if setting else None) or "Master"

    try:
        resp = requests.post(
            f"{url}/api/federation/handshake",
            json={
                "link_token": link_token,
                "master_url": request.host_url,
                "master_name": master_name,
            },
            timeout=10,
            allow_redirects=False,
        )
    except requests.ConnectionError:
        raise _HandshakeError("Cannot connect to child panel")
    except requests.Timeout:
        raise _HandshakeError("Connection to child panel timed out")

    if resp.status_code != 200:
        try:
            err = resp.json().get("error", resp.text)
        except Exception:
            err = resp.text
        raise _HandshakeError(f"Handshake failed: {err}")

    token = str(resp.json().get("federation_token", "") or "")
    if not token:
        raise _HandshakeError("Handshake returned no federation token")
    return token


def _decode_link_token(raw: str) -> tuple[str, str]:

    import base64

    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        if "|" in decoded:
            url, token = decoded.split("|", 1)
            if url.startswith("http"):
                return url.rstrip("/"), token
    except Exception:
        pass
    return "", raw


def _coerce_ip(host):

    import ipaddress
    import socket

    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _private_urls_allowed() -> bool:
    return (os.getenv("FEDERATION_ALLOW_PRIVATE_URLS", "") or "").strip().lower() in ("1", "true", "yes", "on")


def _validate_panel_url(url: str) -> str:

    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Panel URL must use http or https")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError("Panel URL has no host")
    if _private_urls_allowed():
        return url
    ip = _coerce_ip(host)
    if ip is not None:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                "Panel URL resolves to a non-routable address. If the master and this node share a private "
                "network on purpose, set FEDERATION_ALLOW_PRIVATE_URLS=true on the master."
            )
    else:
        low = host.lower()
        if low == "localhost" or "." not in low or low.endswith((".local", ".internal", ".localhost")):
            raise ValueError(
                "Panel URL host is not a public domain. If the master and this node share a private network "
                "on purpose, set FEDERATION_ALLOW_PRIVATE_URLS=true on the master."
            )
    return url


@bp.route("/panels", methods=["GET"])
@token_required
def list_panels():
    panels = LinkedPanel.query.order_by(LinkedPanel.id).all()
    items = [p.to_dict() for p in panels]
    for item in items:
        status, last_poll = get_panel_liveness(item["id"])
        if status:
            item["status"] = status
        if last_poll:
            item["last_poll"] = last_poll
    return jsonify(items), 200


@bp.route("/panels", methods=["POST"])
@token_required
def create_panel():
    try:
        data = request.get_json(silent=True) or {}

        name = str(data.get("name", "") or "").strip()
        if not name:
            raise ValueError("Panel name is required")

        raw_input = str(data.get("link_token", "") or "").strip()
        if not raw_input:
            raise ValueError("Link token is required")

        url, link_token = _decode_link_token(raw_input)
        if not url:
            url = str(data.get("url", "") or "").strip().rstrip("/")
        if not url:
            raise ValueError("Could not determine panel URL from token")
        _validate_panel_url(url)

        dup = LinkedPanel.query.filter_by(name=name).first()
        if dup:
            raise ValueError("Panel name already exists")

        try:
            federation_token = _handshake(url, link_token)
        except _HandshakeError as e:
            return jsonify({"error": e.message}), 502

        panel = LinkedPanel(
            name=name,
            url=url,
            federation_token=federation_token,
            created_at=int(time.time() * 1000),
        )
        db.session.add(panel)
        db.session.commit()

        return jsonify(panel.to_dict()), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/panels/<int:panel_id>", methods=["PUT"])
@token_required
def update_panel(panel_id):
    try:
        panel = db.session.get(LinkedPanel, panel_id)
        if not panel:
            return jsonify({"error": "Panel not found"}), 404

        data = request.get_json(silent=True) or {}

        if "name" in data:
            name = str(data["name"] or "").strip()
            if not name:
                raise ValueError("Panel name is required")
            dup = LinkedPanel.query.filter_by(name=name).first()
            if dup and dup.id != panel_id:
                raise ValueError("Panel name already exists")
            panel.name = name

        if "enable" in data:
            panel.enable = bool(data["enable"])

        db.session.commit()
        return jsonify(panel.to_dict()), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/panels/<int:panel_id>/relink", methods=["POST"])
@token_required
def relink_panel(panel_id):
    try:
        panel = db.session.get(LinkedPanel, panel_id)
        if not panel:
            return jsonify({"error": "Panel not found"}), 404

        data = request.get_json(silent=True) or {}

        raw_input = str(data.get("link_token", "") or "").strip()
        if not raw_input:
            raise ValueError("Link token is required")

        url, link_token = _decode_link_token(raw_input)
        if not url:
            url = (panel.url or "").strip().rstrip("/")
        if not url:
            raise ValueError("Could not determine panel URL from token")
        _validate_panel_url(url)

        try:
            federation_token = _handshake(url, link_token)
        except _HandshakeError as e:
            return jsonify({"error": e.message}), 502

        panel.federation_token = federation_token
        panel.url = url
        db.session.commit()

        _nudge_panel_refresh(panel.id)

        return jsonify(panel.to_dict()), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/panels/<int:panel_id>", methods=["DELETE"])
@token_required
def delete_panel(panel_id):
    try:
        panel = db.session.get(LinkedPanel, panel_id)
        if not panel:
            return jsonify({"error": "Panel not found"}), 404

        purge = purge_tariff_items(TariffItem.panel_id == panel_id)
        db.session.delete(panel)
        db.session.commit()

        forget_panel(panel_id)

        return (
            jsonify(
                {
                    "ok": True,
                    "removed_tariff_items": purge["removed"],
                    "disabled_tariffs": purge["disabled_tariffs"],
                }
            ),
            200,
        )
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/panels/<int:panel_id>/system-stats", methods=["GET"])
@token_required
def panel_system_stats(panel_id):
    panel = db.session.get(LinkedPanel, panel_id)
    if not panel:
        return jsonify({"error": "Panel not found"}), 404
    try:
        resp = requests.get(
            f"{panel.url}/api/stats/system",
            headers={"X-Federation-Token": panel.federation_token},
            timeout=5,
            allow_redirects=False,
        )
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/panels/<int:panel_id>/restart", methods=["POST"])
@token_required
def panel_restart(panel_id):
    panel = db.session.get(LinkedPanel, panel_id)
    if not panel:
        return jsonify({"error": "Panel not found"}), 404
    try:
        resp = requests.post(
            f"{panel.url}/api/restart",
            headers={"X-Federation-Token": panel.federation_token},
            timeout=10,
            allow_redirects=False,
        )
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/panels/<int:panel_id>/backup", methods=["GET"])
@token_required
def panel_backup(panel_id):
    panel = db.session.get(LinkedPanel, panel_id)
    if not panel:
        return jsonify({"error": "Panel not found"}), 404
    try:
        resp = requests.get(
            f"{panel.url}/api/backup",
            headers={"X-Federation-Token": panel.federation_token},
            timeout=300,
            stream=True,
            allow_redirects=False,
        )
        if resp.status_code != 200:
            if resp.status_code == 401:
                return jsonify(
                    {
                        "error": (
                            f"Panel '{panel.name}' rejected this master's federation token. "
                            f"Issue a fresh link token on the node and relink the panel."
                        )
                    }
                ), 401
            try:
                remote_error = resp.json().get("error")
            except Exception:
                remote_error = None
            return jsonify({"error": remote_error or "Backup failed"}), resp.status_code
        from flask import Response

        return Response(
            resp.iter_content(chunk_size=8192),
            content_type=resp.headers.get("Content-Type", "application/octet-stream"),
            headers={"Content-Disposition": resp.headers.get("Content-Disposition", "attachment")},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/panels/<int:panel_id>/restore", methods=["POST"])
@token_required
def panel_restore(panel_id):
    panel = db.session.get(LinkedPanel, panel_id)
    if not panel:
        return jsonify({"error": "Panel not found"}), 404
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    try:
        f = request.files["file"]
        resp = requests.post(
            f"{panel.url}/api/restore",
            headers={"X-Federation-Token": panel.federation_token},
            files={"file": (f.filename, f.stream, f.content_type)},
            timeout=60,
            allow_redirects=False,
        )
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/panels/<int:panel_id>/test", methods=["POST"])
@token_required
def test_panel(panel_id):
    try:
        panel = db.session.get(LinkedPanel, panel_id)
        if not panel:
            return jsonify({"error": "Panel not found"}), 404

        start = time.time()
        try:
            resp = requests.get(
                f"{panel.url}/api/federation/snapshot",
                headers={"X-Federation-Token": panel.federation_token},
                timeout=10,
                allow_redirects=False,
            )
        except requests.ConnectionError:
            panel.status = "offline"
            panel.last_poll = int(time.time() * 1000)
            panel.last_error = "Connection refused"
            db.session.commit()
            result = panel.to_dict()
            result["latency_ms"] = None
            return jsonify(result), 200
        except requests.Timeout:
            panel.status = "offline"
            panel.last_poll = int(time.time() * 1000)
            panel.last_error = "Connection timed out"
            db.session.commit()
            result = panel.to_dict()
            result["latency_ms"] = None
            return jsonify(result), 200

        latency_ms = round((time.time() - start) * 1000)

        if resp.status_code == 200:
            panel.status = "online"
            panel.last_error = None
        else:
            panel.status = "error"
            panel.last_error = f"HTTP {resp.status_code}"

        panel.last_poll = int(time.time() * 1000)
        db.session.commit()

        result = panel.to_dict()
        result["latency_ms"] = latency_ms
        return jsonify(result), 200
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Internal server error"}), 500
