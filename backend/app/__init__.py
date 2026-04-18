import gevent.monkey

gevent.monkey.patch_all()

import os
import re
import time
from urllib.parse import urlparse
from flask import Flask
from flask_cors import CORS
from werkzeug.security import generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from grpc.experimental import gevent as grpc_gevent
from db_migration import migrate_sqlite_db

from .extensions import db, migrate, scheduler, limiter
from .models import Admin, Outbound
from .services.xray import generate_config_file
from .services.stats import sync_traffic_job, check_limits_job, parse_access_logs, cleanup_stats_job
from .services.node_sync import (
    node_health_check_job,
    node_user_sync_job,
    node_inbound_sync_job,
    node_traffic_poll_job,
)

grpc_gevent.init_gevent()
LOCAL_DEV_ORIGINS = [
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:4200",
    "http://127.0.0.1:4200",
]
INSECURE_SECRET_KEY_MARKERS = {"changeme", "change-me", "change_this", "secret"}
ADMIN_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,50}$")


def _extract_host(raw_value):
    candidate = str(raw_value or "").strip()
    if not candidate:
        return ""

    parsed = urlparse(candidate if "://" in candidate else f"//{candidate}")
    if parsed.hostname:
        return parsed.hostname.strip().lower()

    return candidate.split(":", 1)[0].strip().lower()


def _panel_domain_host():
    return _extract_host(os.getenv("PANEL_DOMAIN", ""))


def _is_local_domain(domain):
    if not domain:
        return True
    return domain in {"localhost", "127.0.0.1", "panel.local"} or domain.endswith(".local")


def _cors_origins():
    explicit = os.getenv("CORS_ORIGINS", "").strip()
    if explicit:
        origins = [origin.strip().rstrip("/") for origin in explicit.split(",") if origin.strip()]
        if "*" in origins:
            raise RuntimeError("CORS_ORIGINS cannot contain wildcard '*'.")
        return origins

    panel_host = _panel_domain_host()
    if not panel_host:
        return LOCAL_DEV_ORIGINS

    if _is_local_domain(panel_host):
        local_origins = [f"http://{panel_host}", f"https://{panel_host}"]
        return sorted(set(local_origins + LOCAL_DEV_ORIGINS))

    return [f"https://{panel_host}"]


def _ensure_scheduler_job(job_id, func, seconds):
    if scheduler.get_job(job_id) is None:
        scheduler.add_job(id=job_id, func=func, trigger="interval", seconds=seconds)


def _is_insecure_secret(secret):
    normalized = str(secret or "").strip().lower()
    if not normalized:
        return True
    if len(normalized) < 32:
        return True
    if normalized in INSECURE_SECRET_KEY_MARKERS:
        return True
    if normalized.startswith("change-this") or normalized.startswith("replace-with"):
        return True
    return False


def _is_weak_admin_password(password):
    if len(password) < 8:
        return True
    if not all(32 <= ord(ch) < 127 for ch in password):
        return True
    return False


def _resolve_admin_bootstrap_credentials(panel_host):
    raw_user = os.getenv("PANEL_USER")
    raw_password = os.getenv("PANEL_PASSWORD")

    username = str(raw_user if raw_user is not None else "admin").strip() or "admin"
    password = str(raw_password if raw_password is not None else "admin").strip()

    if not ADMIN_USERNAME_RE.fullmatch(username):
        raise RuntimeError("PANEL_USER must match ^[A-Za-z0-9._-]{1,50}$ and cannot be empty.")
    if not password:
        raise RuntimeError("PANEL_PASSWORD cannot be empty.")

    if _is_local_domain(panel_host):
        return username, password

    if username == "admin" and password == "admin":
        raise RuntimeError("Default admin credentials are not allowed for non-local PANEL_DOMAIN.")
    if _is_weak_admin_password(password):
        raise RuntimeError(
            "PANEL_PASSWORD is too weak for non-local PANEL_DOMAIN. Use at least 8 printable ASCII characters."
        )

    return username, password


def create_app():
    app = Flask(__name__)

    db_folder = os.path.join(os.getcwd(), "db")
    os.makedirs(db_folder, exist_ok=True)

    db_path = os.path.join(db_folder, "panel.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SCHEDULER_API_ENABLED"] = False

    panel_host = _panel_domain_host()
    rate_limit_storage = os.getenv("RATELIMIT_STORAGE_URI", "memory://").strip() or "memory://"
    if not _is_local_domain(panel_host) and rate_limit_storage.startswith("memory://"):
        raise RuntimeError(
            "RATELIMIT_STORAGE_URI must use a persistent backend in production (e.g. redis://redis:6379/0)."
        )
    secret_key = os.getenv("SECRET_KEY", "").strip()
    if not _is_local_domain(panel_host) and _is_insecure_secret(secret_key):
        raise RuntimeError(
            "SECRET_KEY is missing or weak for non-local PANEL_DOMAIN. Use a random value with at least 32 characters."
        )

    CORS(app, resources={r"/api/*": {"origins": _cors_origins()}})
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=2, x_proto=1, x_host=1, x_prefix=1)

    db.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)
    scheduler.init_app(app)

    _ensure_scheduler_job("sync_traffic", sync_traffic_job, 10)
    _ensure_scheduler_job("check_limits", check_limits_job, 60)
    _ensure_scheduler_job("parse_logs", parse_access_logs, 15)
    _ensure_scheduler_job("cleanup_stats", cleanup_stats_job, 86400)  # daily
    _ensure_scheduler_job("node_health_check", node_health_check_job, 60)
    _ensure_scheduler_job("node_user_sync", node_user_sync_job, 3600)
    _ensure_scheduler_job("node_inbound_sync", node_inbound_sync_job, 300)
    _ensure_scheduler_job("node_traffic_poll", node_traffic_poll_job, 60)
    if not scheduler.running:
        scheduler.start()

    from .api import auth, inbound, outbound, routing, system, subscription, statistics, nodes

    app.register_blueprint(auth.bp, url_prefix="/api")
    app.register_blueprint(inbound.bp, url_prefix="/api")
    app.register_blueprint(outbound.bp, url_prefix="/api")
    app.register_blueprint(routing.bp, url_prefix="/api")
    app.register_blueprint(system.bp, url_prefix="/api")
    app.register_blueprint(subscription.bp, url_prefix="/api")
    app.register_blueprint(statistics.bp, url_prefix="/api")
    app.register_blueprint(nodes.bp, url_prefix="/api")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}, 200

    with app.app_context():
        try:
            db.create_all()
            migrate_sqlite_db(db_path, logger=app.logger)
            db.session.remove()
            db.engine.dispose()

            direct_ob = Outbound.query.filter_by(tag="direct").first()
            block_ob = Outbound.query.filter_by(tag="block").first()
            if not direct_ob:
                db.session.add(Outbound(tag="direct", protocol="freedom", enable=True))
            elif not bool(getattr(direct_ob, "enable", True)):
                direct_ob.enable = True
            if not block_ob:
                db.session.add(Outbound(tag="block", protocol="blackhole", enable=True))
            elif not bool(getattr(block_ob, "enable", True)):
                block_ob.enable = True
            db.session.commit()

            generate_config_file()

            if not Admin.query.first():
                user, pw = _resolve_admin_bootstrap_credentials(panel_host)
                if user == "admin" and pw == "admin":
                    app.logger.warning("Security warning: default admin credentials are in use for a local domain.")
                db.session.add(
                    Admin(
                        username=user,
                        password=generate_password_hash(pw),
                        password_changed_at=int(time.time()),
                    )
                )
                db.session.commit()
        except Exception:
            app.logger.exception("Startup error")
            raise

    return app
