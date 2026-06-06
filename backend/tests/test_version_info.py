import datetime
import json

import jwt
import pytest

from app.extensions import db
from app.models import Admin, SystemSetting
from app.services import bot_status
from app.utils import SECRET_KEY
from app.version import get_app_version

BOT_TOKEN = "secret-bot-token"


def _make_token(admin):
    return jwt.encode(
        {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": int(admin.password_changed_at or 0),
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=2),
        },
        SECRET_KEY,
        algorithm="HS256",
    )


@pytest.fixture
def app(app):
    """Register the blueprints this file exercises (idempotent)."""
    from app.api import system as system_api
    from app.api import bot_service as bot_service_api

    if "system" not in app.blueprints:
        app.register_blueprint(system_api.bp, url_prefix="/api")
    if "bot_service" not in app.blueprints:
        app.register_blueprint(bot_service_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(app):
    a = Admin(id=1, username="admin", password="hashed", password_changed_at=0)
    db.session.add(a)
    db.session.commit()
    return a


@pytest.fixture
def auth_headers(admin):
    return {"Authorization": f"Bearer {_make_token(admin)}"}


@pytest.fixture
def bot_headers(app):
    db.session.add(SystemSetting(key="bot_service_token", value=BOT_TOKEN))
    db.session.commit()
    return {"Authorization": f"Bearer {BOT_TOKEN}"}


def test_reads_backend_field_from_given_file(tmp_path):
    p = tmp_path / "versions.json"
    p.write_text(json.dumps({"backend": "9.9.9", "bot": "1.2.3"}))
    assert get_app_version(str(p)) == "9.9.9"


def test_missing_file_falls_back_to_dev(tmp_path):
    assert get_app_version(str(tmp_path / "nope.json")) == "dev"


def test_bot_status_records_and_reads_fresh(monkeypatch):
    monkeypatch.setattr(bot_status.time, "time", lambda: 1000.0)
    bot_status.record_bot_version("2.1.3")
    monkeypatch.setattr(bot_status.time, "time", lambda: 1010.0)  # 10s later
    s = bot_status.get_bot_status(freshness=180)
    assert s == {"version": "2.1.3", "reported_at": 1000.0}


def test_bot_status_stale_returns_none(monkeypatch):
    monkeypatch.setattr(bot_status.time, "time", lambda: 1000.0)
    bot_status.record_bot_version("2.1.3")
    monkeypatch.setattr(bot_status.time, "time", lambda: 1300.0)  # 300s later
    assert bot_status.get_bot_status(freshness=180) == {"version": None, "reported_at": None}


def test_bot_status_ignores_blank(monkeypatch):
    monkeypatch.setattr(bot_status.time, "time", lambda: 1000.0)
    bot_status._STATE["version"] = None
    bot_status._STATE["reported_at"] = None
    bot_status.record_bot_version("")
    assert bot_status.get_bot_status() == {"version": None, "reported_at": None}


def test_runtime_config_records_bot_version(client, bot_headers):
    from app.services import bot_status

    bot_status._STATE["version"] = None
    bot_status._STATE["reported_at"] = None
    resp = client.get(
        "/api/bot/runtime-config",
        headers={**bot_headers, "X-Bot-Version": "2.1.3"},
    )
    assert resp.status_code == 200
    assert bot_status.get_bot_status()["version"] == "2.1.3"


def test_version_check_populates_cache_on_success(monkeypatch):
    from app.services import version_check

    version_check._CACHE["latest"] = None
    version_check._CACHE["checked_at"] = None
    monkeypatch.setattr(version_check.time, "time", lambda: 5000.0)
    monkeypatch.setattr(
        version_check,
        "_http_get_json",
        lambda url, timeout=5: {"backend": "9.9.9", "bot": "1.0.0"},
    )
    version_check.fetch_latest()
    got = version_check.get_latest()
    assert got["latest"]["backend"] == "9.9.9"
    assert got["checked_at"] == 5000.0


def test_version_check_keeps_previous_on_failure(monkeypatch):
    from app.services import version_check

    version_check._CACHE["latest"] = {"backend": "1.1.1"}
    version_check._CACHE["checked_at"] = 1.0

    def boom(url, timeout=5):
        raise OSError("network down")

    monkeypatch.setattr(version_check, "_http_get_json", boom)
    version_check.fetch_latest()  # must not raise
    assert version_check.get_latest()["latest"] == {"backend": "1.1.1"}


def test_version_endpoint_shape(client, auth_headers, monkeypatch):
    from app.services import bot_status, version_check

    monkeypatch.setattr("app.api.system.get_app_version", lambda: "2.1.10")
    version_check._CACHE["latest"] = {"backend": "2.1.11", "bot": "2.1.3"}
    version_check._CACHE["checked_at"] = 4242.0
    bot_status._STATE["version"] = None
    bot_status._STATE["reported_at"] = None

    resp = client.get("/api/system/version", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["running"]["backend"] == "2.1.10"
    assert body["running"]["bot"] is None  # not reported
    assert body["latest"]["backend"] == "2.1.11"
    assert body["latest_checked_at"] == 4242.0
