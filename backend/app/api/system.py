import psutil
import datetime
import os
import signal
import sqlite3
import shutil
import threading
import time
import json
from flask import Blueprint, after_this_request, jsonify, request, send_file, Response, stream_with_context
from app.utils import token_required, admin_or_bot_token_required
from app.extensions import limiter, db
from app.models import Client, SystemSetting
from app.services.xray import (
    restart_xray_container,
    update_geo_db,
    stream_xray_logs,
    generate_config_file,
    get_system_settings,
    normalize_geo_data_url,
    normalize_xray_log_level,
)

bp = Blueprint("system", __name__)
MAX_RESTORE_DB_BYTES = 50 * 1024 * 1024
ALLOWED_BACKUP_EXTENSIONS = (".db", ".sqlite", ".sqlite3")
SQLITE_HEADER = b"SQLite format 3\x00"
DB_FILENAME = "panel.db"
DB_BACKUP_SUFFIX = ".bak"


def _db_path():
    return os.path.join(os.getcwd(), "db", DB_FILENAME)


def _ensure_db_folder():
    db_folder = os.path.dirname(_db_path())
    os.makedirs(db_folder, exist_ok=True)
    return db_folder


def _validate_sqlite_backup(path):
    if not os.path.exists(path):
        return "Backup file is missing"

    if os.path.getsize(path) > MAX_RESTORE_DB_BYTES:
        return "Backup file is too large"

    try:
        with open(path, "rb") as file_obj:
            if file_obj.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                return "Unsupported backup format"
    except OSError:
        return "Failed to read uploaded backup"

    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute("PRAGMA integrity_check;").fetchone()
            status = str(row[0]).strip().lower() if row else ""
            if status != "ok":
                return "Backup integrity check failed"
    except sqlite3.DatabaseError:
        return "Backup integrity check failed"

    return None


def _schedule_worker_restart(delay_seconds=1):
    def _restart():
        time.sleep(delay_seconds)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_restart, daemon=True).start()


def _set_system_setting(key, value):
    item = SystemSetting.query.filter_by(key=key).first()
    if item:
        item.value = str(value)
    else:
        db.session.add(SystemSetting(key=key, value=str(value)))


@bp.route("/stats/system", methods=["GET"])
@admin_or_bot_token_required
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


@bp.route("/stats/users", methods=["GET"])
@token_required
def get_users_stats():
    try:
        total = Client.query.count()
        active = Client.query.filter_by(enable=True).count()
        return jsonify({"total": total, "active": active})
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/restart", methods=["POST"])
@admin_or_bot_token_required
@limiter.limit("5 per minute")
def restart():
    try:
        restart_xray_container()
        return jsonify({"status": "restarted"}), 200
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/logs", methods=["GET"])
@token_required
def get_logs():
    def generate():
        for line in stream_xray_logs():
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
    try:
        return jsonify(get_system_settings())
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/system/settings", methods=["PUT"])
@token_required
@limiter.limit("20 per minute")
def system_settings_update():
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
    from app.services.xray import (
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
    try:
        config_path = "/etc/xray/config.json"
        if not os.path.exists(config_path):
            return jsonify({"error": "Config file not found"}), 404

        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data)
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/backup", methods=["GET"])
@token_required
@limiter.limit("30 per minute")
def backup():
    import tempfile

    db_path = _db_path()
    if not os.path.exists(db_path):
        return jsonify({"error": "DB not found"}), 404
    db.session.commit()
    # Use SQLite Backup API to create a consistent snapshot.
    # send_file(db_path) would silently miss any committed data still
    # in the WAL file (panel.db-wal); backup() goes through the SQLite
    # engine and captures the full logical database regardless of WAL state.
    # The snapshot is streamed from disk (not buffered in RAM) and deleted
    # via after_this_request once Flask is done sending the response.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", dir=os.path.dirname(db_path))
    os.close(tmp_fd)
    try:
        with sqlite3.connect(db_path, timeout=10.0) as src, sqlite3.connect(tmp_path) as dst:
            src.backup(dst)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    @after_this_request
    def _cleanup(response):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return response

    return send_file(
        tmp_path,
        as_attachment=True,
        download_name=f"backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.db",
        mimetype="application/octet-stream",
    )


@bp.route("/restore", methods=["POST"])
@token_required
@limiter.limit("5 per hour")
def restore():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selection"}), 400

    from werkzeug.utils import secure_filename

    safe_name = secure_filename(file.filename or "").lower()
    if not safe_name or not safe_name.endswith(ALLOWED_BACKUP_EXTENSIONS):
        return jsonify({"error": "Unsupported backup format"}), 400

    temp_path = ""
    replaced = False
    try:
        db_folder = _ensure_db_folder()
        db_path = _db_path()
        backup_path = f"{db_path}{DB_BACKUP_SUFFIX}"
        temp_path = os.path.join(db_folder, f".restore-{int(time.time() * 1000)}.tmp")

        file.save(temp_path)
        validation_error = _validate_sqlite_backup(temp_path)
        if validation_error:
            return jsonify({"error": validation_error}), 400

        db.session.remove()
        db.engine.dispose()

        if os.path.exists(db_path):
            shutil.copy2(db_path, backup_path)

        os.replace(temp_path, db_path)
        replaced = True

        generate_config_file()
        restart_xray_container()
        _schedule_worker_restart()

        return jsonify({"status": "restored"}), 200
    except Exception:
        if replaced:
            backup_path = f"{_db_path()}{DB_BACKUP_SUFFIX}"
            if os.path.exists(backup_path):
                try:
                    os.replace(backup_path, _db_path())
                except OSError:
                    pass
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


@bp.route("/system/update-geo", methods=["POST"])
@token_required
@limiter.limit("10 per hour")
def geo_update():
    try:
        update_geo_db()
        return jsonify({"status": "updated"}), 200
    except Exception:
        return jsonify({"error": "Internal server error"}), 500
