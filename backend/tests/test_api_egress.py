import time

import jwt
import pytest

from panel_core.extensions import db
from panel_core.models import Admin
from panel_core.services.egress import build_bind_ips  # noqa: F401
from panel_core.utils import SECRET_KEY


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


def test_host_script_route_is_gone(client, auth_headers, app):
    """§8.11: the endpoint was removed from every role, not gated.

    The frontend button lived behind a hasLocalXray guard while the route was open to any
    admin JWT — a gate closed at one end only. Both ends are gone now; this pins the API half.
    """
    assert client.get("/api/system/egress/host-script", headers=auth_headers).status_code == 404
    rules = {str(r.rule) for r in app.url_map.iter_rules()}
    assert "/api/system/egress/host-script" not in rules
    assert "/api/system/egress/bind-ips" in rules, "the sidecar endpoint must survive the removal"


def test_host_plan_requires_the_egress_token(client, monkeypatch):
    monkeypatch.setenv("EGRESS_INTERNAL_TOKEN", "s3cret")
    assert client.get("/api/system/egress/host-plan").status_code == 403
    ok = client.get("/api/system/egress/host-plan", headers={"X-Egress-Token": "s3cret"})
    assert ok.status_code == 200
    assert isinstance(ok.get_json(), list)


def test_host_plan_503_when_token_unset(client, monkeypatch):
    monkeypatch.delenv("EGRESS_INTERNAL_TOKEN", raising=False)
    assert client.get("/api/system/egress/host-plan", headers={"X-Egress-Token": "x"}).status_code == 503


def test_host_plan_is_not_reachable_with_an_admin_jwt_alone(client, auth_headers, monkeypatch):
    monkeypatch.setenv("EGRESS_INTERNAL_TOKEN", "s3cret")
    assert client.get("/api/system/egress/host-plan", headers=auth_headers).status_code == 403, (
        "the plan is read by a host daemon that holds the egress token and nothing else. "
        "Accepting an admin JWT here would widen the surface for no consumer."
    )


def test_bind_ips_keeps_its_own_shape(client, monkeypatch, app):
    monkeypatch.setenv("EGRESS_INTERNAL_TOKEN", "s3cret")
    body = client.get("/api/system/egress/bind-ips", headers={"X-Egress-Token": "s3cret"}).get_json()
    assert all(set(row) == {"send_through", "prefix"} for row in body), (
        "the sidecar inside Xray's netns parses this with jq and needs no host fields. "
        "Growing it into the host plan would tie two independent consumers to one format."
    )
    rules = {str(r.rule) for r in app.url_map.iter_rules()}
    assert "/api/system/egress/bind-ips" in rules
    assert "/api/system/egress/host-plan" in rules
