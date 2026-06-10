import time

import jwt
import pytest

from app.extensions import db
from app.models import Admin, SystemSetting
from app.utils import SECRET_KEY


@pytest.fixture
def app(app):

    from app.api import bot_admin

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


def test_get_settings_returns_secret(app, client, admin_token):
    with app.app_context():
        db.session.add_all(
            [
                SystemSetting(key="yookassa_shop_id", value="shop1"),
                SystemSetting(key="yookassa_secret_key", value="secret-x"),
                SystemSetting(key="yookassa_return_url", value="https://t.me/itg"),
            ]
        )
        db.session.commit()
    resp = client.get(
        "/api/bot/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["yookassa_shop_id"] == "shop1"
    assert body["yookassa_return_url"] == "https://t.me/itg"
    assert body["has_yookassa_secret"] is True
    assert body["yookassa_secret_key"] == "secret-x"


def test_get_settings_indicates_missing_secret(client, admin_token):
    resp = client.get(
        "/api/bot/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    body = resp.get_json()
    assert body["has_yookassa_secret"] is False
    assert body["yookassa_secret_key"] == ""
    assert body["yookassa_shop_id"] == ""


def test_put_settings_persists(app, client, admin_token):
    resp = client.put(
        "/api/bot/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "yookassa_shop_id": "newshop",
            "yookassa_secret_key": "newsecret",
            "yookassa_return_url": "https://example.test/r",
        },
    )
    assert resp.status_code == 200
    with app.app_context():

        def _val(k):
            r = SystemSetting.query.filter_by(key=k).first()
            return r.value if r else None

        assert _val("yookassa_shop_id") == "newshop"
        assert _val("yookassa_secret_key") == "newsecret"
        assert _val("yookassa_return_url") == "https://example.test/r"


def test_put_settings_partial_update(app, client, admin_token):
    with app.app_context():
        db.session.add(SystemSetting(key="yookassa_secret_key", value="keep-me"))
        db.session.commit()
    resp = client.put(
        "/api/bot/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"yookassa_shop_id": "only-shop"},
    )
    assert resp.status_code == 200
    with app.app_context():

        def _val(k):
            r = SystemSetting.query.filter_by(key=k).first()
            return r.value if r else None

        assert _val("yookassa_shop_id") == "only-shop"
        assert _val("yookassa_secret_key") == "keep-me"


def test_put_settings_does_not_overwrite_secret_with_empty_string(app, client, admin_token):
    with app.app_context():
        db.session.add(SystemSetting(key="yookassa_secret_key", value="keep-me"))
        db.session.commit()
    resp = client.put(
        "/api/bot/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"yookassa_secret_key": ""},
    )
    assert resp.status_code == 200
    with app.app_context():
        row = SystemSetting.query.filter_by(key="yookassa_secret_key").first()
        assert row.value == "keep-me"


def test_device_settings_round_trip(app, client, admin_token):
    resp = client.put(
        "/api/bot/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"device_limit_enabled": True, "device_limit_per_user": 3},
    )
    assert resp.status_code == 200
    got = client.get("/api/bot/settings", headers={"Authorization": f"Bearer {admin_token}"}).get_json()
    assert got["device_limit_enabled"] is True
    assert got["device_limit_per_user"] == 3


def test_device_settings_disable_and_zero_persist(app, client, admin_token):
    client.put(
        "/api/bot/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"device_limit_enabled": True, "device_limit_per_user": 5},
    )
    resp = client.put(
        "/api/bot/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"device_limit_enabled": False, "device_limit_per_user": 0},
    )
    assert resp.status_code == 200
    got = client.get("/api/bot/settings", headers={"Authorization": f"Bearer {admin_token}"}).get_json()
    assert got["device_limit_enabled"] is False
    assert got["device_limit_per_user"] == 0


def test_device_limit_per_user_rejects_negative(app, client, admin_token):
    resp = client.put(
        "/api/bot/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"device_limit_per_user": -1},
    )
    assert resp.status_code == 400


def test_device_settings_defaults_when_unset(app, client, admin_token):
    got = client.get("/api/bot/settings", headers={"Authorization": f"Bearer {admin_token}"}).get_json()
    assert got["device_limit_enabled"] is False
    assert got["device_limit_per_user"] == 0
