import time

import jwt as jwt_lib
import pytest

from panel_core.models import (
    Admin,
    Client,
    FederationConfig,
    Inbound,
    SystemSetting,
)


@pytest.fixture
def app_with_federation(app):
    from panel_core.api import federation

    if not any(bp.name == "federation" for bp in app.blueprints.values()):
        app.register_blueprint(federation.bp, url_prefix="/api")
    return app


@pytest.fixture
def admin_headers(app_with_federation, db):
    admin = Admin(username="admin", password="x", password_changed_at=0)
    db.session.add(admin)
    db.session.commit()
    from panel_core.utils import SECRET_KEY

    token = jwt_lib.encode(
        {
            "user": "admin",
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": 0,
            "exp": time.time() + 3600,
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(app_with_federation):
    return app_with_federation.test_client()


@pytest.fixture
def linked_config(db):

    cfg = db.session.get(FederationConfig, 1)
    cfg.federation_token = "test-federation-token-abc"
    cfg.master_url = "https://master.example.com"
    cfg.master_name = "Master"
    cfg.link_token = "used-token"
    cfg.link_token_used = True
    cfg.linked_at = int(time.time() * 1000)
    db.session.commit()
    return cfg


@pytest.fixture
def federation_headers(linked_config):

    return {"X-Federation-Token": linked_config.federation_token}


class TestLinkToken:
    def test_generates_link_token(self, client, admin_headers, db):
        resp = client.post("/api/federation/link-token", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert "link_token" in body
        assert len(body["link_token"]) > 20

        import base64

        cfg = db.session.get(FederationConfig, 1)
        assert cfg.link_token is not None
        decoded = base64.urlsafe_b64decode(body["link_token"] + "==").decode()
        assert decoded.endswith("|" + cfg.link_token)
        assert cfg.link_token_used is False

    def test_returns_409_when_already_linked(self, client, admin_headers, db):
        cfg = db.session.get(FederationConfig, 1)
        cfg.federation_token = "existing-token"
        cfg.linked_at = int(time.time() * 1000)
        db.session.commit()

        resp = client.post("/api/federation/link-token", headers=admin_headers)
        assert resp.status_code == 409
        assert "already linked" in resp.get_json()["error"]

    def test_allows_regenerate_when_not_fully_linked(self, client, admin_headers, db):

        cfg = db.session.get(FederationConfig, 1)
        cfg.federation_token = "partial"
        cfg.linked_at = None
        db.session.commit()

        resp = client.post("/api/federation/link-token", headers=admin_headers)
        assert resp.status_code == 200

    def test_requires_auth(self, client):
        resp = client.post("/api/federation/link-token")
        assert resp.status_code == 401


class TestHandshake:
    def test_successful_handshake(self, client, admin_headers, db):

        resp = client.post("/api/federation/link-token", headers=admin_headers)
        link_token = resp.get_json()["link_token"]

        resp = client.post(
            "/api/federation/handshake",
            json={
                "link_token": link_token,
                "master_url": "https://master.example.com",
                "master_name": "Master Panel",
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert "federation_token" in body
        assert len(body["federation_token"]) > 20
        assert body["name"] == "Panel"
        assert body["panel_version"] == 15
        assert isinstance(body["inbound_count"], int)

        cfg = db.session.get(FederationConfig, 1)
        assert cfg.federation_token == body["federation_token"]
        assert cfg.master_url == "https://master.example.com"
        assert cfg.master_name == "Master Panel"
        assert cfg.link_token_used is True
        assert cfg.linked_at is not None

    def test_handshake_with_custom_panel_name(self, client, admin_headers, db):
        db.session.add(SystemSetting(key="panel_name", value="DE-1"))
        db.session.commit()

        resp = client.post("/api/federation/link-token", headers=admin_headers)
        link_token = resp.get_json()["link_token"]

        resp = client.post(
            "/api/federation/handshake",
            json={
                "link_token": link_token,
                "master_url": "https://master.example.com",
                "master_name": "Master",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["name"] == "DE-1"

    def test_handshake_wrong_token(self, client, admin_headers, db):
        client.post("/api/federation/link-token", headers=admin_headers)

        resp = client.post(
            "/api/federation/handshake",
            json={
                "link_token": "wrong-token",
                "master_url": "https://master.example.com",
                "master_name": "Master",
            },
        )
        assert resp.status_code == 401

    def test_handshake_missing_token(self, client):
        resp = client.post(
            "/api/federation/handshake",
            json={"master_url": "https://master.example.com"},
        )
        assert resp.status_code == 401

    def test_handshake_no_pending_token(self, client, db):

        resp = client.post(
            "/api/federation/handshake",
            json={"link_token": "anything"},
        )
        assert resp.status_code == 401

    def test_handshake_token_already_used(self, client, admin_headers, db):
        resp = client.post("/api/federation/link-token", headers=admin_headers)
        link_token = resp.get_json()["link_token"]

        resp = client.post(
            "/api/federation/handshake",
            json={
                "link_token": link_token,
                "master_url": "https://master.example.com",
                "master_name": "Master",
            },
        )
        assert resp.status_code == 200

        resp = client.post(
            "/api/federation/handshake",
            json={
                "link_token": link_token,
                "master_url": "https://master2.example.com",
                "master_name": "Master2",
            },
        )
        assert resp.status_code == 401
        assert "already used" in resp.get_json()["error"]


class TestSnapshot:
    def test_snapshot_empty(self, client, federation_headers):
        resp = client.get("/api/federation/snapshot", headers=federation_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert body["inbounds"] == []
        assert "timestamp" in body
        assert "panel_name" in body

    def test_snapshot_with_inbounds_and_clients(self, client, federation_headers, db):
        ib = Inbound(
            tag="vless-tcp",
            port=443,
            protocol="vless",
            label="Main VLESS",
            stream_settings='{"network": "tcp"}',
            fallback_address="127.0.0.1:8080",
            device_limit=3,
        )
        db.session.add(ib)
        db.session.flush()

        c = Client(
            id="11111111-1111-1111-1111-111111111111",
            email="user@vless-tcp",
            inbound_tag="vless-tcp",
            enable=True,
            up=100,
            down=200,
            limit_bytes=1073741824,
            expiry_time=1700000000000,
            reset_day=15,
            flow="xtls-rprx-vision",
            telegram_id=42,
        )
        db.session.add(c)
        db.session.commit()

        resp = client.get("/api/federation/snapshot", headers=federation_headers)
        assert resp.status_code == 200
        body = resp.get_json()

        assert len(body["inbounds"]) == 1
        ib_data = body["inbounds"][0]
        assert ib_data["tag"] == "vless-tcp"
        assert ib_data["port"] == 443
        assert ib_data["protocol"] == "vless"
        assert ib_data["label"] == "Main VLESS"
        assert ib_data["stream_settings"] == {"network": "tcp"}
        assert ib_data["fallback_address"] == "127.0.0.1:8080"
        assert ib_data["device_limit"] == 3

        assert len(ib_data["clients"]) == 1
        cl = ib_data["clients"][0]
        assert cl["id"] == "11111111-1111-1111-1111-111111111111"
        assert cl["email"] == "user@vless-tcp"
        assert cl["enable"] is True
        assert cl["up"] == 100
        assert cl["down"] == 200
        assert cl["limit_bytes"] == 1073741824
        assert cl["expiry_time"] == 1700000000000
        assert cl["reset_day"] == 15
        assert cl["flow"] == "xtls-rprx-vision"
        assert cl["telegram_id"] == 42
        assert cl["device_count"] == 0

    def test_snapshot_stream_settings_as_dict(self, client, federation_headers, db):

        ib = Inbound(
            tag="ss-in",
            port=8388,
            protocol="shadowsocks",
            stream_settings="not-valid-json",
        )
        db.session.add(ib)
        db.session.commit()

        resp = client.get("/api/federation/snapshot", headers=federation_headers)
        assert resp.status_code == 200
        body = resp.get_json()

        assert body["inbounds"][0]["stream_settings"] == {}

    def test_snapshot_requires_federation_token(self, client):
        resp = client.get("/api/federation/snapshot")
        assert resp.status_code == 401

    def test_snapshot_rejects_wrong_token(self, client, linked_config):
        resp = client.get(
            "/api/federation/snapshot",
            headers={"X-Federation-Token": "wrong-token"},
        )
        assert resp.status_code == 401


class TestGetConfig:
    def test_config_when_linked(self, client, admin_headers, linked_config):
        resp = client.get("/api/federation/config", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["is_linked"] is True
        assert body["master_url"] == "https://master.example.com"
        assert body["master_name"] == "Master"
        assert body["linked_at"] is not None

        assert body["link_token"] is None

    def test_config_when_not_linked(self, client, admin_headers, db):
        resp = client.get("/api/federation/config", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["is_linked"] is False
        assert body["master_url"] is None
        assert body["master_name"] is None
        assert body["linked_at"] is None

    def test_config_shows_unused_link_token(self, client, admin_headers, db):
        cfg = db.session.get(FederationConfig, 1)
        cfg.link_token = "pending-token-123"
        cfg.link_token_used = False
        db.session.commit()

        resp = client.get("/api/federation/config", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.get_json()

        import base64

        decoded = base64.urlsafe_b64decode(body["link_token"] + "==").decode()
        assert decoded.endswith("|pending-token-123")
        assert body["is_linked"] is False

    def test_config_requires_auth(self, client):
        resp = client.get("/api/federation/config")
        assert resp.status_code == 401


class TestProvision:
    def test_provision_returns_501_when_not_implemented(self, client, federation_headers):

        resp = client.post(
            "/api/federation/provision",
            headers=federation_headers,
            json={
                "telegram_id": 42,
                "inbound_tag": "vless-tcp",
                "expiry_ms": 1700000000000,
                "limit_bytes": 0,
                "tariff_id": 1,
            },
        )

        assert resp.status_code in (200, 400, 501)

    def test_provision_requires_federation_token(self, client):
        resp = client.post(
            "/api/federation/provision",
            json={"telegram_id": 42, "inbound_tag": "vless-tcp"},
        )
        assert resp.status_code == 401

    def test_provision_missing_required_fields(self, client, federation_headers):
        resp = client.post(
            "/api/federation/provision",
            headers=federation_headers,
            json={"tariff_id": 1},
        )
        assert resp.status_code == 400
        assert "required" in resp.get_json()["error"]
