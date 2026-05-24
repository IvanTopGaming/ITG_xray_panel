"""Verify sensitive system endpoints reject the bot service token.

/backup exfiltrates the entire SQLite DB; /restore replaces it. Neither is
needed by the bot, and accepting the bot service token there means
compromising one container yields full DB read+write.
"""

import time

import jwt
import pytest

from app.extensions import db
from app.models import Admin, SystemSetting
from app.utils import SECRET_KEY


@pytest.fixture
def app(app):
    from app.api import system as system_api

    if not any(bp.name == "system" for bp in app.blueprints.values()):
        app.register_blueprint(system_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def bot_token(app):
    with app.app_context():
        db.session.add(SystemSetting(key="bot_service_token", value="bot-secret"))
        db.session.commit()
    return "bot-secret"


@pytest.fixture
def admin_token(app):
    with app.app_context():
        pwd_version = int(time.time())
        admin = Admin(
            username="admin",
            password="hashed",
            password_changed_at=pwd_version,
        )
        db.session.add(admin)
        db.session.commit()
        return jwt.encode(
            {
                "user": admin.username,
                "admin_id": admin.id,
                "role": "admin",
                "pwdv": pwd_version,
                "exp": int(time.time()) + 3600,
            },
            SECRET_KEY,
            algorithm="HS256",
        )


def test_backup_rejects_bot_service_token(client, bot_token):
    resp = client.get(
        "/api/backup",
        headers={"Authorization": f"Bearer {bot_token}"},
    )
    assert resp.status_code == 401


def test_restore_rejects_bot_service_token(client, bot_token):
    resp = client.post(
        "/api/restore",
        headers={"Authorization": f"Bearer {bot_token}"},
        data={},
    )
    assert resp.status_code == 401


def test_backup_accepts_admin_jwt(client, admin_token, tmp_path, monkeypatch):
    """Admin token still works (sanity check that we didn't break the admin path)."""
    monkeypatch.setattr("app.api.system._db_path", lambda: str(tmp_path / "nope.db"))
    resp = client.get(
        "/api/backup",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    # _db_path returns a non-existent file → endpoint returns 404, but it
    # at least passed the auth gate (no 401).
    assert resp.status_code != 401
