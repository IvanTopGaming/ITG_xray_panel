import datetime
import logging
import os
import shutil
import signal
import sqlite3
import threading
import time

from flask import Blueprint, after_this_request, current_app, g, jsonify, request, send_file

from panel_core.db_config import is_postgres
from panel_core.extensions import db, limiter
from panel_core.utils import admin_or_federation_token_required
from panel_core.xray.facade import generate_config_file, restart_xray_container

logger = logging.getLogger(__name__)

bp = Blueprint("backup", __name__)

MAX_RESTORE_DB_BYTES = 50 * 1024 * 1024
ALLOWED_BACKUP_EXTENSIONS = (".db", ".sqlite", ".sqlite3")
SQLITE_HEADER = b"SQLite format 3\x00"
DB_FILENAME = "panel.db"
DB_BACKUP_SUFFIX = ".bak"
POSTGRES_BACKUP_UNSUPPORTED = (
    "This panel keeps its data in Postgres, and the panel backup reads a SQLite file. "
    "Back the data tier up with the pg-backup container from docker-compose.postgres.yml instead."
)


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


def _postgres_refusal():
    if is_postgres(current_app.config.get("SQLALCHEMY_DATABASE_URI", "")):
        return jsonify({"error": POSTGRES_BACKUP_UNSUPPORTED}), 409
    return None


def _audit(action):
    source = request.remote_addr or "an unknown address"
    if getattr(g, "auth_via", None) == "federation":
        logger.warning("%s over the federation token from %s", action, source)
    else:
        logger.info("%s by a panel admin from %s", action, source)


@bp.route("/backup", methods=["GET"])
@admin_or_federation_token_required
@limiter.limit("30 per minute")
def backup():
    import tempfile

    refusal = _postgres_refusal()
    if refusal is not None:
        return refusal

    db_path = _db_path()
    if not os.path.exists(db_path):
        return jsonify({"error": "DB not found"}), 404
    db.session.commit()

    _audit("database backup requested")

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
@admin_or_federation_token_required
@limiter.limit("5 per hour")
def restore():
    refusal = _postgres_refusal()
    if refusal is not None:
        return refusal

    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selection"}), 400

    from werkzeug.utils import secure_filename

    safe_name = secure_filename(file.filename or "").lower()
    if not safe_name or not safe_name.endswith(ALLOWED_BACKUP_EXTENSIONS):
        return jsonify({"error": "Unsupported backup format"}), 400

    _audit("database restore requested")

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
