import time

import jwt
import pytest

from panel_core.models import Admin, SystemSetting
from panel_core.utils import SECRET_KEY


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
def admin_headers(app, db):
    pwd_version = int(time.time())
    admin = Admin(username="admin", password="x", password_changed_at=pwd_version)
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
    return {"Authorization": f"Bearer {token}"}


def test_settings_round_trip_through_the_admin_api(client, db, admin_headers):
    resp = client.put(
        "/api/bot/settings",
        json={
            "brand_name": "ITG VPN",
            "subscription_update_interval_hours": 6,
            "panel_name": "Frankfurt master",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200

    stored = {row.key: row.value for row in SystemSetting.query.all()}
    assert stored["brand_name"] == "ITG VPN"
    assert stored["subscription_update_interval_hours"] == "6"
    assert stored["panel_name"] == "Frankfurt master"

    body = client.get("/api/bot/settings", headers=admin_headers).get_json()
    assert body["brand_name"] == "ITG VPN"
    assert body["subscription_update_interval_hours"] == 6
    assert body["panel_name"] == "Frankfurt master"


def test_interval_must_be_a_positive_integer(client, db, admin_headers):
    for bad in (0, -1, "soon"):
        resp = client.put(
            "/api/bot/settings",
            json={"subscription_update_interval_hours": bad},
            headers=admin_headers,
        )
        assert resp.status_code == 400, bad


def test_an_absurdly_long_name_is_refused(client, db, admin_headers):
    resp = client.put(
        "/api/bot/settings",
        json={"brand_name": "x" * 65},
        headers=admin_headers,
    )
    assert resp.status_code == 400


def test_what_the_master_writes_is_what_the_subscription_role_reads(client, db, admin_headers):
    from panel_core.api.subscription import _update_interval_hours

    client.put(
        "/api/bot/settings",
        json={"subscription_update_interval_hours": 12},
        headers=admin_headers,
    )

    assert _update_interval_hours() == 12


def test_defaults_are_reported_when_nothing_was_ever_saved(client, db, admin_headers):
    body = client.get("/api/bot/settings", headers=admin_headers).get_json()

    assert body["brand_name"] == ""
    assert body["panel_name"] == ""
    assert body["subscription_update_interval_hours"] == 24
