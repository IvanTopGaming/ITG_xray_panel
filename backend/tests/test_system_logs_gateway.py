import time

import jwt
import pytest

from panel_core.extensions import db
from panel_core.models import Admin
from panel_core.utils import SECRET_KEY
from panel_core.xray import gateway as gw


@pytest.fixture
def app(app):
    from panel_core.api import system as system_api

    if not any(bp.name == "system" for bp in app.blueprints.values()):
        app.register_blueprint(system_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth(app):
    with app.app_context():
        pwd_version = int(time.time())
        admin = Admin(username="admin", password="hashed", password_changed_at=pwd_version)
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


def test_logs_returns_501_without_a_local_xray(client, auth):
    from panel_core.api.system import XRAY_LOGS_UNSUPPORTED

    gw.set_xray_gateway(gw.RemoteXrayGateway())

    resp = client.get("/api/logs", headers=auth)

    assert resp.status_code == 501
    assert resp.mimetype == "application/json"
    assert resp.get_json() == {"error": XRAY_LOGS_UNSUPPORTED}


def test_logs_streams_when_the_gateway_can_tail(client, auth):
    class _Gateway(gw.NullXrayGateway):
        def stream_logs(self, tail_lines=0):
            return iter(["first\n", "second\n"])

    gw.set_xray_gateway(_Gateway())

    resp = client.get("/api/logs", headers=auth)

    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert resp.get_data(as_text=True) == "data: first\n\ndata: second\n\n"


def test_logs_is_an_empty_stream_on_a_null_gateway(client, auth):
    gw.set_xray_gateway(gw.NullXrayGateway())

    resp = client.get("/api/logs", headers=auth)

    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert resp.get_data(as_text=True) == ""
