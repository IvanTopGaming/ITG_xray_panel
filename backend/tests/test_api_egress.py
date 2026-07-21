import time

import jwt
import pytest

from app.extensions import db
from app.models import Admin
from app.services.egress import build_bind_ips  # noqa: F401
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


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def test_bind_ips_requires_token(client, monkeypatch):
    monkeypatch.setenv("EGRESS_INTERNAL_TOKEN", "s3cret")
    assert client.get("/api/system/egress/bind-ips").status_code == 403
    ok = client.get("/api/system/egress/bind-ips", headers={"X-Egress-Token": "s3cret"})
    assert ok.status_code == 200
    assert isinstance(ok.get_json(), list)


def test_bind_ips_503_when_token_unset(client, monkeypatch):
    monkeypatch.delenv("EGRESS_INTERNAL_TOKEN", raising=False)
    assert client.get("/api/system/egress/bind-ips", headers={"X-Egress-Token": "x"}).status_code == 503


def test_host_script_admin_only(client, auth_headers):
    assert client.get("/api/system/egress/host-script").status_code == 401
    resp = client.get("/api/system/egress/host-script", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert b"EGRESS_SNAT" in resp.data


def test_host_script_bad_iface_returns_400(client, auth_headers):
    resp = client.get("/api/system/egress/host-script?iface=bad;stuff", headers=auth_headers)
    assert resp.status_code == 400
