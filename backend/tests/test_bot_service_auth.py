import time

import jwt
import pytest
from flask import jsonify

from panel_core.extensions import db
from panel_core.models import Admin, SystemSetting
from panel_core.utils import SECRET_KEY, bot_service_token_required


@pytest.fixture
def app_with_route(app):

    @bot_service_token_required
    def _ping():
        return jsonify({"ok": True})

    app.add_url_rule("/_test/ping", view_func=_ping, methods=["GET"])
    return app


def test_missing_token_returns_401(app_with_route, db):
    db.session.add(SystemSetting(key="bot_service_token", value="secret-abc"))
    db.session.commit()
    client = app_with_route.test_client()
    resp = client.get("/_test/ping")
    assert resp.status_code == 401


def test_wrong_token_returns_401(app_with_route, db):
    db.session.add(SystemSetting(key="bot_service_token", value="secret-abc"))
    db.session.commit()
    client = app_with_route.test_client()
    resp = client.get("/_test/ping", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_correct_token_returns_200(app_with_route, db):
    db.session.add(SystemSetting(key="bot_service_token", value="secret-abc"))
    db.session.commit()
    client = app_with_route.test_client()
    resp = client.get("/_test/ping", headers={"Authorization": "Bearer secret-abc"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_no_token_setting_returns_500(app_with_route, db):

    client = app_with_route.test_client()
    resp = client.get("/_test/ping", headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 500


@pytest.fixture
def app(app):

    from panel_core.api import bot_admin

    if not any(bp.name == "bot_admin" for bp in app.blueprints.values()):
        app.register_blueprint(bot_admin.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_token(app, db):
    pwd_version = int(time.time())
    admin = Admin(
        username="admin",
        password="hashed-not-checked-by-token-required",
        password_changed_at=pwd_version,
    )
    db.session.add(admin)
    db.session.commit()
    token = jwt.encode(
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
    return token


def test_rotate_bot_service_token_replaces_value(app, client, admin_token):

    with app.app_context():
        db.session.add(SystemSetting(key="bot_service_token", value="old-value"))
        db.session.commit()
    resp = client.post(
        "/api/bot/settings/rotate-bot-service-token",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    new_token = resp.get_json()["token"]
    assert new_token and new_token != "old-value"
    with app.app_context():
        row = SystemSetting.query.filter_by(key="bot_service_token").first()
        assert row.value == new_token
