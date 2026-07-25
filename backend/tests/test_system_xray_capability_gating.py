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


def test_restart_returns_501_without_a_local_xray(client, auth):
    from panel_core.api.system import XRAY_RESTART_UNSUPPORTED

    gw.set_xray_gateway(gw.RemoteXrayGateway())

    resp = client.post("/api/restart", headers=auth)

    assert resp.status_code == 501
    assert resp.get_json() == {"error": XRAY_RESTART_UNSUPPORTED}


def test_geo_update_returns_501_without_a_local_xray(client, auth):
    from panel_core.api.system import XRAY_GEO_UNSUPPORTED

    gw.set_xray_gateway(gw.RemoteXrayGateway())

    resp = client.post("/api/system/update-geo", headers=auth)

    assert resp.status_code == 501
    assert resp.get_json() == {"error": XRAY_GEO_UNSUPPORTED}


def test_restart_still_restarts_when_a_local_xray_exists(client, auth):
    calls = []

    class _Gateway(gw.NullXrayGateway):
        def has_local_xray(self):
            return True

        def restart(self):
            calls.append("restart")

    gw.set_xray_gateway(_Gateway())

    resp = client.post("/api/restart", headers=auth)

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "restarted"}
    assert calls == ["restart"]


def test_geo_update_still_updates_when_a_local_xray_exists(client, auth):
    calls = []

    class _Gateway(gw.NullXrayGateway):
        def has_local_xray(self):
            return True

        def update_geo(self):
            calls.append("geo")

    gw.set_xray_gateway(_Gateway())

    resp = client.post("/api/system/update-geo", headers=auth)

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "updated"}
    assert calls == ["geo"]


def test_logs_returns_501_without_a_local_xray(client, auth):
    from panel_core.api.system import XRAY_LOGS_UNSUPPORTED

    gw.set_xray_gateway(gw.RemoteXrayGateway())

    resp = client.get("/api/logs", headers=auth)

    assert resp.status_code == 501
    assert resp.mimetype == "application/json"
    assert resp.get_json() == {"error": XRAY_LOGS_UNSUPPORTED}


def test_logs_returns_501_on_a_null_gateway(client, auth):
    from panel_core.api.system import XRAY_LOGS_UNSUPPORTED

    gw.set_xray_gateway(gw.NullXrayGateway())

    resp = client.get("/api/logs", headers=auth)

    assert resp.status_code == 501
    assert resp.get_json() == {"error": XRAY_LOGS_UNSUPPORTED}


def test_logs_streams_when_the_gateway_has_a_local_xray(client, auth):
    class _Gateway(gw.NullXrayGateway):
        def has_local_xray(self):
            return True

        def stream_logs(self, tail_lines=0):
            return iter(["first\n", "second\n"])

    gw.set_xray_gateway(_Gateway())

    resp = client.get("/api/logs", headers=auth)

    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert resp.get_data(as_text=True) == "data: first\n\ndata: second\n\n"


def test_logs_reports_a_late_raise_before_committing_sse_headers(client, auth):
    from panel_core.api.system import XRAY_LOGS_UNSUPPORTED

    class _LyingGateway(gw.NullXrayGateway):
        def has_local_xray(self):
            return True

        def stream_logs(self, tail_lines=0):
            raise gw.LocalXrayUnavailable("no xray after all")

    gw.set_xray_gateway(_LyingGateway())

    resp = client.get("/api/logs", headers=auth)

    assert resp.status_code == 501
    assert resp.mimetype == "application/json"
    assert resp.get_json() == {"error": XRAY_LOGS_UNSUPPORTED}
