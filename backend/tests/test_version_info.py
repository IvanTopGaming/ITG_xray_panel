import datetime
import json
import os

import jwt
import pytest

from panel_core.extensions import db
from panel_core.models import Admin, SystemSetting
from panel_core.services import bot_status
from panel_core.utils import SECRET_KEY
from panel_core import version as version_module
from panel_core.version import get_app_version

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

    from panel_core.api import system as system_api
    from panel_core.api import bot_service as bot_service_api

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


def test_reads_the_running_roles_version_from_a_given_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PANEL_ROLE", raising=False)
    p = tmp_path / "versions.json"
    p.write_text(json.dumps({"master": "9.9.9", "bot": "1.2.3"}))
    assert get_app_version(str(p)) == "9.9.9"


def test_missing_file_falls_back_to_dev(tmp_path):
    assert get_app_version(str(tmp_path / "nope.json")) == "dev"


def test_fallback_candidates_resolve_to_repo_root_versions_json():
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    expected = os.path.join(repo_root, "versions.json")
    resolved = [os.path.abspath(p) for p in version_module._CANDIDATES]
    assert expected in resolved
    assert os.path.exists(expected)


def test_bot_status_records_and_reads_fresh(monkeypatch):
    monkeypatch.setattr(bot_status.time, "time", lambda: 1000.0)
    bot_status.record_bot_version("2.1.3")
    monkeypatch.setattr(bot_status.time, "time", lambda: 1010.0)
    s = bot_status.get_bot_status(freshness=180)
    assert s == {"version": "2.1.3", "reported_at": 1000.0}


def test_bot_status_stale_returns_none(monkeypatch):
    monkeypatch.setattr(bot_status.time, "time", lambda: 1000.0)
    bot_status.record_bot_version("2.1.3")
    monkeypatch.setattr(bot_status.time, "time", lambda: 1300.0)
    assert bot_status.get_bot_status(freshness=180) == {"version": None, "reported_at": None}


def test_bot_status_ignores_blank(monkeypatch):
    monkeypatch.setattr(bot_status.time, "time", lambda: 1000.0)
    bot_status._STATE["version"] = None
    bot_status._STATE["reported_at"] = None
    bot_status.record_bot_version("")
    assert bot_status.get_bot_status() == {"version": None, "reported_at": None}


def test_runtime_config_records_bot_version(client, bot_headers):
    from panel_core.services import bot_status

    bot_status._STATE["version"] = None
    bot_status._STATE["reported_at"] = None
    resp = client.get(
        "/api/bot/runtime-config",
        headers={**bot_headers, "X-Bot-Version": "2.1.3"},
    )
    assert resp.status_code == 200
    assert bot_status.get_bot_status()["version"] == "2.1.3"


def test_version_check_populates_cache_on_success(monkeypatch):
    from panel_core.services import version_check

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
    from panel_core.services import version_check

    version_check._CACHE["latest"] = {"backend": "1.1.1"}
    version_check._CACHE["checked_at"] = 1.0

    def boom(url, timeout=5):
        raise OSError("network down")

    monkeypatch.setattr(version_check, "_http_get_json", boom)
    version_check.fetch_latest()
    assert version_check.get_latest()["latest"] == {"backend": "1.1.1"}


def test_version_key_is_mapped_per_role_not_taken_from_the_role_name():
    from panel_core.panel_role import ROLE_BOT, ROLE_MASTER, ROLE_SUB, ROLE_WORKER
    from panel_core.version import VERSION_KEY_BY_ROLE

    assert VERSION_KEY_BY_ROLE == {
        ROLE_MASTER: "master",
        ROLE_WORKER: "worker",
        ROLE_SUB: "sub",
        ROLE_BOT: "bot_api",
    }
    assert VERSION_KEY_BY_ROLE[ROLE_BOT] != ROLE_BOT, (
        "the bot-api role is named 'bot', and versions.json already has a 'bot' key holding the "
        "TELEGRAM BOT's version. Using the role name as the version key would make bot-api report the "
        "Telegram bot's version. The mapping must stay explicit."
    )


@pytest.mark.parametrize(
    "role,expected",
    [("master", "2.0.0"), ("worker", "2.0.1"), ("sub", "2.0.2"), ("bot", "2.0.3")],
)
def test_each_role_reports_its_own_image_version(role, expected, tmp_path, monkeypatch):
    import json

    from panel_core.version import get_app_version

    path = tmp_path / "versions.json"
    path.write_text(
        json.dumps({"master": "2.0.0", "worker": "2.0.1", "sub": "2.0.2", "bot_api": "2.0.3", "bot": "9.9.9"})
    )
    monkeypatch.setenv("PANEL_ROLE", role)
    assert get_app_version(str(path)) == expected


def test_a_missing_key_reports_dev_rather_than_another_roles_version(tmp_path, monkeypatch):
    import json

    from panel_core.version import get_app_version

    path = tmp_path / "versions.json"
    path.write_text(json.dumps({"master": "2.0.0"}))
    monkeypatch.setenv("PANEL_ROLE", "sub")
    assert get_app_version(str(path)) == "dev"


def test_versions_json_declares_the_four_backend_images_and_no_legacy_backend_key():
    import json
    import pathlib

    from panel_core.version import VERSION_KEY_BY_ROLE

    root = pathlib.Path(__file__).resolve().parents[2]
    assert (root / "backend").is_dir(), f"{root} is not the repo root — the parents[2] index drifted"
    data = json.loads((root / "versions.json").read_text())

    missing = sorted(key for key in VERSION_KEY_BY_ROLE.values() if key not in data)
    assert missing == [], f"versions.json is missing backend image versions: {missing}"

    assert "backend" not in data, (
        "versions.json still carries the legacy single 'backend' key. Four per-role images replaced it; "
        "leaving it means the release workflow would build a fifth image nothing deploys, and "
        "get_app_version() would silently prefer it."
    )


def test_version_endpoint_shape(client, auth_headers, monkeypatch):
    from panel_core.services import bot_status, version_check
    from panel_core.version import app_version_key

    monkeypatch.setenv("PANEL_ROLE", "worker")
    monkeypatch.setattr("panel_core.api.system.get_app_version", lambda: "2.1.10")
    version_check._CACHE["latest"] = {"backend": "2.1.11", "bot": "2.1.3"}
    version_check._CACHE["checked_at"] = 4242.0
    bot_status._STATE["version"] = None
    bot_status._STATE["reported_at"] = None

    resp = client.get("/api/system/version", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["running"]["backend"] == "2.1.10"
    assert body["running"]["backend_key"] == app_version_key()
    assert body["running"]["bot"] is None
    assert body["latest"]["backend"] == "2.1.11"
    assert body["latest_checked_at"] == 4242.0
