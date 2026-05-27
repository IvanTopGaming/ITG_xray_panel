import time

import requests
from flask import Blueprint, request, jsonify

from app.extensions import db, get_redis
from app.models import LinkedPanel, SystemSetting
from app.utils import token_required, admin_or_bot_token_required

bp = Blueprint("panels", __name__)


def _decode_link_token(raw: str) -> tuple[str, str]:
    """Decode a composite link token (base64 of 'url|token') into (url, token).

    Falls back to ('', raw) if the input is a plain token without embedded URL.
    """
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


@bp.route("/panels", methods=["GET"])
@admin_or_bot_token_required
def list_panels():
    panels = LinkedPanel.query.order_by(LinkedPanel.id).all()
    return jsonify([p.to_dict() for p in panels]), 200


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

        dup = LinkedPanel.query.filter_by(name=name).first()
        if dup:
            raise ValueError("Panel name already exists")

        # Resolve master's own name from SystemSetting
        setting = SystemSetting.query.filter_by(key="panel_name").first()
        master_name = (setting.value if setting else None) or "Master"

        # Perform handshake with child panel
        try:
            resp = requests.post(
                f"{url}/api/federation/handshake",
                json={
                    "link_token": link_token,
                    "master_url": request.host_url,
                    "master_name": master_name,
                },
                timeout=10,
            )
        except requests.ConnectionError:
            return jsonify({"error": "Cannot connect to child panel"}), 502
        except requests.Timeout:
            return jsonify({"error": "Connection to child panel timed out"}), 502

        if resp.status_code != 200:
            try:
                err = resp.json().get("error", resp.text)
            except Exception:
                err = resp.text
            return jsonify({"error": f"Handshake failed: {err}"}), 502

        resp_data = resp.json()
        federation_token = resp_data.get("federation_token", "")

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


@bp.route("/panels/<int:panel_id>", methods=["DELETE"])
@token_required
def delete_panel(panel_id):
    try:
        panel = db.session.get(LinkedPanel, panel_id)
        if not panel:
            return jsonify({"error": "Panel not found"}), 404

        db.session.delete(panel)
        db.session.commit()

        # Clean up Redis cache keys
        redis = get_redis()
        if redis:
            try:
                redis.delete(f"panel:{panel_id}:snapshot", f"panel:{panel_id}:status")
            except Exception:
                pass

        return jsonify({"ok": True}), 200
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/panels/<int:panel_id>/system-stats", methods=["GET"])
@admin_or_bot_token_required
def panel_system_stats(panel_id):
    panel = db.session.get(LinkedPanel, panel_id)
    if not panel:
        return jsonify({"error": "Panel not found"}), 404
    try:
        resp = requests.get(
            f"{panel.url}/api/stats/system",
            headers={"X-Federation-Token": panel.federation_token},
            timeout=5,
        )
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/panels/<int:panel_id>/restart", methods=["POST"])
@admin_or_bot_token_required
def panel_restart(panel_id):
    panel = db.session.get(LinkedPanel, panel_id)
    if not panel:
        return jsonify({"error": "Panel not found"}), 404
    try:
        resp = requests.post(
            f"{panel.url}/api/restart",
            headers={"X-Federation-Token": panel.federation_token},
            timeout=10,
        )
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/panels/<int:panel_id>/backup", methods=["GET"])
@admin_or_bot_token_required
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
        )
        if resp.status_code != 200:
            return jsonify({"error": "Backup failed"}), resp.status_code
        from flask import Response

        return Response(
            resp.iter_content(chunk_size=8192),
            content_type=resp.headers.get("Content-Type", "application/octet-stream"),
            headers={"Content-Disposition": resp.headers.get("Content-Disposition", "attachment")},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/panels/<int:panel_id>/restore", methods=["POST"])
@admin_or_bot_token_required
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
        )
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/panels/<int:panel_id>/test", methods=["POST"])
@admin_or_bot_token_required
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
            )
        except requests.ConnectionError:
            panel.status = "offline"
            panel.last_poll = int(time.time())
            panel.last_error = "Connection refused"
            db.session.commit()
            result = panel.to_dict()
            result["latency_ms"] = None
            return jsonify(result), 200
        except requests.Timeout:
            panel.status = "offline"
            panel.last_poll = int(time.time())
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

        panel.last_poll = int(time.time())
        db.session.commit()

        result = panel.to_dict()
        result["latency_ms"] = latency_ms
        return jsonify(result), 200
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Internal server error"}), 500
