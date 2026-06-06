"""Tests for /api/outbounds and /api/balancers endpoints (outbound blueprint)."""

import datetime
import json
from unittest.mock import patch

import jwt
import pytest

from app.extensions import db
from app.models import Admin, Outbound, Balancer, Client, Inbound
from app.utils import SECRET_KEY


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_token(admin):
    pwd_version = int(admin.password_changed_at or 0)
    return jwt.encode(
        {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": pwd_version,
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=2),
        },
        SECRET_KEY,
        algorithm="HS256",
    )


@pytest.fixture
def app(app):
    """Extend base app fixture with the outbound blueprint."""
    from app.api import outbound as outbound_api

    if not any(bp_name == "outbound" for bp_name in app.blueprints):
        app.register_blueprint(outbound_api.bp, url_prefix="/api")
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
def token(admin):
    return _make_token(admin)


@pytest.fixture
def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def seed_outbounds(app):
    """Seed the two system outbounds plus one custom outbound."""
    db.session.add(Outbound(tag="direct", protocol="freedom", enable=True))
    db.session.add(Outbound(tag="block", protocol="blackhole", enable=True))
    db.session.add(
        Outbound(
            tag="proxy-de",
            protocol="socks",
            enable=True,
            settings=json.dumps({"servers": [{"address": "1.2.3.4", "port": 1080}]}),
        )
    )
    db.session.commit()


# ---------------------------------------------------------------------------
# Xray mocks — applied to every test automatically
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_xray():
    with (
        patch("app.api.outbound.generate_config_file"),
        patch("app.api.outbound.restart_xray_container"),
    ):
        yield


# ===========================================================================
# Auth
# ===========================================================================


class TestAuth:
    def test_get_outbounds_requires_token(self, client):
        resp = client.get("/api/outbounds")
        assert resp.status_code == 401

    def test_post_outbound_requires_token(self, client):
        resp = client.post("/api/outbounds", json={"tag": "x", "protocol": "freedom"})
        assert resp.status_code == 401

    def test_put_outbound_requires_token(self, client):
        resp = client.put("/api/outbounds/direct", json={"protocol": "freedom"})
        assert resp.status_code == 401

    def test_delete_outbound_requires_token(self, client):
        resp = client.delete("/api/outbounds/direct")
        assert resp.status_code == 401

    def test_get_balancers_requires_token(self, client):
        resp = client.get("/api/balancers")
        assert resp.status_code == 401

    def test_post_balancer_requires_token(self, client):
        resp = client.post("/api/balancers", json={"tag": "b", "selector": ["direct"]})
        assert resp.status_code == 401

    def test_put_balancer_requires_token(self, client):
        resp = client.put("/api/balancers/b", json={"strategy": "random"})
        assert resp.status_code == 401

    def test_delete_balancer_requires_token(self, client):
        resp = client.delete("/api/balancers/b")
        assert resp.status_code == 401

    def test_get_outbounds_health_requires_token(self, client):
        resp = client.get("/api/outbounds/health")
        assert resp.status_code == 401


# ===========================================================================
# GET /api/outbounds
# ===========================================================================


class TestGetOutbounds:
    def test_empty_list(self, client, auth_headers, admin):
        resp = client.get("/api/outbounds", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_seeded_outbounds(self, client, auth_headers, admin, seed_outbounds):
        resp = client.get("/api/outbounds", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        tags = {o["tag"] for o in data}
        assert tags == {"direct", "block", "proxy-de"}

    def test_outbound_shape(self, client, auth_headers, admin, seed_outbounds):
        resp = client.get("/api/outbounds", headers=auth_headers)
        item = next(o for o in resp.get_json() if o["tag"] == "direct")
        assert set(item.keys()) == {"tag", "protocol", "enable", "settings", "streamSettings", "mux"}
        assert item["protocol"] == "freedom"
        assert item["enable"] is True


# ===========================================================================
# GET /api/outbounds/health
# ===========================================================================


class TestGetOutboundsHealth:
    def test_health_unknown_for_freedom(self, client, auth_headers, admin, seed_outbounds):
        resp = client.get("/api/outbounds/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        direct = next(o for o in data if o["tag"] == "direct")
        assert direct["status"] == "unknown"

    def test_health_probes_socks(self, client, auth_headers, admin, seed_outbounds):
        """The socks outbound has a valid address:port, so it should be probed."""
        with patch("app.api.outbound._probe_outbound", return_value=42):
            resp = client.get("/api/outbounds/health", headers=auth_headers)
        data = resp.get_json()
        proxy = next(o for o in data if o["tag"] == "proxy-de")
        assert proxy["status"] == "up"
        assert proxy["rttMs"] == 42
        assert proxy["endpoint"] == "1.2.3.4:1080"

    def test_health_probe_failure(self, client, auth_headers, admin, seed_outbounds):
        with patch("app.api.outbound._probe_outbound", side_effect=OSError("refused")):
            resp = client.get("/api/outbounds/health", headers=auth_headers)
        data = resp.get_json()
        proxy = next(o for o in data if o["tag"] == "proxy-de")
        assert proxy["status"] == "down"
        assert "refused" in proxy["error"]


# ===========================================================================
# POST /api/outbounds
# ===========================================================================


class TestCreateOutbound:
    def test_create_freedom(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "my-freedom", "protocol": "freedom"},
        )
        assert resp.status_code == 201
        assert resp.get_json()["tag"] == "my-freedom"

    def test_create_blackhole(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "my-block", "protocol": "blackhole"},
        )
        assert resp.status_code == 201

    def test_create_socks(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={
                "tag": "socks-proxy",
                "protocol": "socks",
                "settings": {"servers": [{"address": "1.2.3.4", "port": 1080}]},
            },
        )
        assert resp.status_code == 201

    def test_create_with_all_fields(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={
                "tag": "full",
                "protocol": "vmess",
                "enable": False,
                "settings": {"vnext": []},
                "streamSettings": {"network": "tcp"},
                "mux": {"enabled": True, "concurrency": 8},
            },
        )
        assert resp.status_code == 201
        # Verify stored correctly
        ob = Outbound.query.filter_by(tag="full").first()
        assert ob.enable is False
        assert json.loads(ob.stream_settings) == {"network": "tcp"}
        assert json.loads(ob.mux) == {"enabled": True, "concurrency": 8}

    def test_duplicate_tag_rejected(self, client, auth_headers, admin, seed_outbounds):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "proxy-de", "protocol": "socks"},
        )
        assert resp.status_code == 400
        assert "exists" in resp.get_json()["error"].lower()

    def test_reserved_tag_api(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "api", "protocol": "freedom"},
        )
        assert resp.status_code == 400
        assert "reserved" in resp.get_json()["error"].lower()

    def test_reserved_tag_direct(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "direct", "protocol": "freedom"},
        )
        assert resp.status_code == 400
        assert "reserved" in resp.get_json()["error"].lower()

    def test_reserved_tag_block(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "block", "protocol": "blackhole"},
        )
        assert resp.status_code == 400
        assert "reserved" in resp.get_json()["error"].lower()

    def test_empty_tag_rejected(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "", "protocol": "freedom"},
        )
        assert resp.status_code == 400

    def test_empty_protocol_rejected(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "x", "protocol": ""},
        )
        assert resp.status_code == 400

    def test_settings_must_be_object(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "x", "protocol": "freedom", "settings": "not-an-object"},
        )
        assert resp.status_code == 400
        assert "object" in resp.get_json()["error"].lower()

    def test_stream_settings_must_be_object(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "x", "protocol": "freedom", "streamSettings": [1, 2]},
        )
        assert resp.status_code == 400

    def test_mux_must_be_object(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "x", "protocol": "freedom", "mux": 42},
        )
        assert resp.status_code == 400


# ===========================================================================
# Config-validation gate: validate before persisting to the DB
# ===========================================================================


class TestCreateOutboundValidationGate:
    """generate_config_file() runs before db.session.commit(); a rejected config
    must return 400 and leave NO row committed (no poisoned rows)."""

    def test_rejected_config_does_not_persist(self, client, auth_headers, admin):
        with patch(
            "app.api.outbound.generate_config_file",
            side_effect=ValueError("Xray rejected the config: boom"),
        ):
            resp = client.post(
                "/api/outbounds",
                headers=auth_headers,
                json={
                    "tag": "test-reject",
                    "protocol": "socks",
                    "settings": {"servers": [{"address": "1.2.3.4", "port": 1080}]},
                },
            )
        assert resp.status_code == 400
        assert "rejected" in resp.get_json()["error"].lower()
        assert Outbound.query.filter_by(tag="test-reject").first() is None

    def test_accepted_config_persists(self, client, auth_headers, admin):
        with patch("app.api.outbound.generate_config_file"):
            resp = client.post(
                "/api/outbounds",
                headers=auth_headers,
                json={
                    "tag": "test-reject",
                    "protocol": "socks",
                    "settings": {"servers": [{"address": "1.2.3.4", "port": 1080}]},
                },
            )
        assert resp.status_code == 201
        assert Outbound.query.filter_by(tag="test-reject").first() is not None


# ===========================================================================
# PUT /api/outbounds/<tag>
# ===========================================================================


class TestUpdateOutbound:
    def test_update_protocol(self, client, auth_headers, admin, seed_outbounds):
        resp = client.put(
            "/api/outbounds/proxy-de",
            headers=auth_headers,
            json={"protocol": "http"},
        )
        assert resp.status_code == 200
        assert Outbound.query.filter_by(tag="proxy-de").first().protocol == "http"

    def test_update_settings(self, client, auth_headers, admin, seed_outbounds):
        new_settings = {"servers": [{"address": "5.6.7.8", "port": 443}]}
        resp = client.put(
            "/api/outbounds/proxy-de",
            headers=auth_headers,
            json={"settings": new_settings},
        )
        assert resp.status_code == 200
        ob = Outbound.query.filter_by(tag="proxy-de").first()
        assert json.loads(ob.settings) == new_settings

    def test_update_stream_settings(self, client, auth_headers, admin, seed_outbounds):
        resp = client.put(
            "/api/outbounds/proxy-de",
            headers=auth_headers,
            json={"streamSettings": {"network": "ws"}},
        )
        assert resp.status_code == 200

    def test_update_mux(self, client, auth_headers, admin, seed_outbounds):
        resp = client.put(
            "/api/outbounds/proxy-de",
            headers=auth_headers,
            json={"mux": {"enabled": True}},
        )
        assert resp.status_code == 200

    def test_update_enable(self, client, auth_headers, admin, seed_outbounds):
        resp = client.put(
            "/api/outbounds/proxy-de",
            headers=auth_headers,
            json={"enable": False},
        )
        assert resp.status_code == 200
        assert Outbound.query.filter_by(tag="proxy-de").first().enable is False

    def test_cannot_disable_system_outbound_direct(self, client, auth_headers, admin, seed_outbounds):
        resp = client.put(
            "/api/outbounds/direct",
            headers=auth_headers,
            json={"enable": False},
        )
        assert resp.status_code == 400
        assert "cannot be modified" in resp.get_json()["error"].lower()

    def test_cannot_disable_system_outbound_block(self, client, auth_headers, admin, seed_outbounds):
        resp = client.put(
            "/api/outbounds/block",
            headers=auth_headers,
            json={"enable": False},
        )
        assert resp.status_code == 400

    def test_cannot_change_protocol_of_direct(self, client, auth_headers, admin, seed_outbounds):
        resp = client.put(
            "/api/outbounds/direct",
            headers=auth_headers,
            json={"protocol": "blackhole"},
        )
        assert resp.status_code == 400
        assert "cannot be modified" in resp.get_json()["error"].lower()
        assert Outbound.query.filter_by(tag="direct").first().protocol == "freedom"

    def test_cannot_change_settings_of_block(self, client, auth_headers, admin, seed_outbounds):
        resp = client.put(
            "/api/outbounds/block",
            headers=auth_headers,
            json={"settings": {"servers": [{"address": "1.2.3.4", "port": 443}]}},
        )
        assert resp.status_code == 400
        assert json.loads(Outbound.query.filter_by(tag="block").first().settings) == {}

    def test_cannot_change_stream_settings_of_direct(self, client, auth_headers, admin, seed_outbounds):
        resp = client.put(
            "/api/outbounds/direct",
            headers=auth_headers,
            json={"streamSettings": {"network": "ws"}},
        )
        assert resp.status_code == 400

    def test_cannot_change_mux_of_direct(self, client, auth_headers, admin, seed_outbounds):
        resp = client.put(
            "/api/outbounds/direct",
            headers=auth_headers,
            json={"mux": {"enabled": True}},
        )
        assert resp.status_code == 400

    def test_update_nonexistent_returns_404(self, client, auth_headers, admin):
        resp = client.put(
            "/api/outbounds/no-such",
            headers=auth_headers,
            json={"protocol": "freedom"},
        )
        assert resp.status_code == 404

    def test_update_settings_must_be_object(self, client, auth_headers, admin, seed_outbounds):
        resp = client.put(
            "/api/outbounds/proxy-de",
            headers=auth_headers,
            json={"settings": "bad"},
        )
        assert resp.status_code == 400

    def test_partial_update_keeps_other_fields(self, client, auth_headers, admin, seed_outbounds):
        """Updating protocol should not reset settings."""
        ob_before = Outbound.query.filter_by(tag="proxy-de").first()
        settings_before = ob_before.settings

        client.put(
            "/api/outbounds/proxy-de",
            headers=auth_headers,
            json={"protocol": "http"},
        )
        ob_after = Outbound.query.filter_by(tag="proxy-de").first()
        assert ob_after.protocol == "http"
        assert ob_after.settings == settings_before


# ===========================================================================
# DELETE /api/outbounds/<tag>
# ===========================================================================


class TestDeleteOutbound:
    def test_delete_custom_outbound(self, client, auth_headers, admin, seed_outbounds):
        resp = client.delete("/api/outbounds/proxy-de", headers=auth_headers)
        assert resp.status_code == 200
        assert Outbound.query.filter_by(tag="proxy-de").first() is None

    def test_cannot_delete_direct(self, client, auth_headers, admin, seed_outbounds):
        resp = client.delete("/api/outbounds/direct", headers=auth_headers)
        assert resp.status_code == 400
        assert "system" in resp.get_json()["error"].lower()

    def test_cannot_delete_block(self, client, auth_headers, admin, seed_outbounds):
        resp = client.delete("/api/outbounds/block", headers=auth_headers)
        assert resp.status_code == 400

    def test_delete_nonexistent_returns_404(self, client, auth_headers, admin):
        resp = client.delete("/api/outbounds/no-such", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_blocked_by_balancer_selector(self, client, auth_headers, admin, seed_outbounds):
        db.session.add(
            Balancer(
                tag="bal1",
                selector=json.dumps(["proxy-de"]),
                strategy="random",
            )
        )
        db.session.commit()
        resp = client.delete("/api/outbounds/proxy-de", headers=auth_headers)
        assert resp.status_code == 400
        assert "selector" in resp.get_json()["error"].lower()

    def test_delete_blocked_by_balancer_fallback(self, client, auth_headers, admin, seed_outbounds):
        db.session.add(
            Balancer(
                tag="bal2",
                selector=json.dumps(["direct"]),
                strategy="random",
                fallback_tag="proxy-de",
            )
        )
        db.session.commit()
        resp = client.delete("/api/outbounds/proxy-de", headers=auth_headers)
        assert resp.status_code == 400
        assert "fallback" in resp.get_json()["error"].lower()

    def test_delete_clears_client_preferred_outbound(self, client, auth_headers, admin, seed_outbounds):
        """Deleting an outbound should null out preferred_outbound on clients that referenced it."""
        inbound = Inbound(tag="vless-in", port=10000, protocol="vless", stream_settings="{}")
        db.session.add(inbound)
        db.session.flush()
        c = Client(
            id="uuid-1",
            email="user1",
            inbound_tag="vless-in",
            preferred_outbound="proxy-de",
        )
        db.session.add(c)
        db.session.commit()

        resp = client.delete("/api/outbounds/proxy-de", headers=auth_headers)
        assert resp.status_code == 200
        assert db.session.get(Client, "uuid-1").preferred_outbound is None


# ===========================================================================
# GET /api/balancers
# ===========================================================================


class TestGetBalancers:
    def test_empty_list(self, client, auth_headers, admin):
        resp = client.get("/api/balancers", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_balancers(self, client, auth_headers, admin, seed_outbounds):
        db.session.add(
            Balancer(
                tag="bal-a",
                selector=json.dumps(["direct", "proxy-de"]),
                strategy="random",
                enable=True,
                fallback_tag=None,
            )
        )
        db.session.commit()
        resp = client.get("/api/balancers", headers=auth_headers)
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["tag"] == "bal-a"
        assert set(data[0].keys()) == {"tag", "enable", "selector", "strategy", "fallback_tag"}
        assert data[0]["selector"] == ["direct", "proxy-de"]


# ===========================================================================
# POST /api/balancers
# ===========================================================================


class TestCreateBalancer:
    def test_create_basic(self, client, auth_headers, admin, seed_outbounds):
        resp = client.post(
            "/api/balancers",
            headers=auth_headers,
            json={"tag": "my-bal", "selector": ["direct", "proxy-de"]},
        )
        assert resp.status_code == 201
        assert resp.get_json()["tag"] == "my-bal"
        bal = Balancer.query.filter_by(tag="my-bal").first()
        assert bal.strategy == "random"
        assert bal.enable is True

    def test_create_with_strategy(self, client, auth_headers, admin, seed_outbounds):
        resp = client.post(
            "/api/balancers",
            headers=auth_headers,
            json={"tag": "b1", "selector": ["direct"], "strategy": "leastPing"},
        )
        assert resp.status_code == 201
        assert Balancer.query.filter_by(tag="b1").first().strategy == "leastPing"

    def test_create_with_fallback(self, client, auth_headers, admin, seed_outbounds):
        resp = client.post(
            "/api/balancers",
            headers=auth_headers,
            json={
                "tag": "b2",
                "selector": ["proxy-de"],
                "fallback_tag": "direct",
            },
        )
        assert resp.status_code == 201
        assert Balancer.query.filter_by(tag="b2").first().fallback_tag == "direct"

    def test_create_disabled(self, client, auth_headers, admin, seed_outbounds):
        resp = client.post(
            "/api/balancers",
            headers=auth_headers,
            json={"tag": "b3", "selector": ["direct"], "enable": False},
        )
        assert resp.status_code == 201
        assert Balancer.query.filter_by(tag="b3").first().enable is False

    def test_duplicate_tag_rejected(self, client, auth_headers, admin, seed_outbounds):
        client.post(
            "/api/balancers",
            headers=auth_headers,
            json={"tag": "dup", "selector": ["direct"]},
        )
        resp = client.post(
            "/api/balancers",
            headers=auth_headers,
            json={"tag": "dup", "selector": ["direct"]},
        )
        assert resp.status_code == 400
        assert "exists" in resp.get_json()["error"].lower()

    def test_reserved_tag_rejected(self, client, auth_headers, admin, seed_outbounds):
        resp = client.post(
            "/api/balancers",
            headers=auth_headers,
            json={"tag": "system_auto_balancer", "selector": ["direct"]},
        )
        assert resp.status_code == 400
        assert "reserved" in resp.get_json()["error"].lower()

    def test_empty_selector_rejected(self, client, auth_headers, admin, seed_outbounds):
        resp = client.post(
            "/api/balancers",
            headers=auth_headers,
            json={"tag": "b-empty", "selector": []},
        )
        assert resp.status_code == 400
        assert "empty" in resp.get_json()["error"].lower()

    def test_unknown_selector_tag_rejected(self, client, auth_headers, admin, seed_outbounds):
        resp = client.post(
            "/api/balancers",
            headers=auth_headers,
            json={"tag": "b-bad", "selector": ["nonexistent"]},
        )
        assert resp.status_code == 400
        assert "unknown" in resp.get_json()["error"].lower()

    def test_invalid_strategy_rejected(self, client, auth_headers, admin, seed_outbounds):
        resp = client.post(
            "/api/balancers",
            headers=auth_headers,
            json={"tag": "b-strat", "selector": ["direct"], "strategy": "fastest"},
        )
        assert resp.status_code == 400
        assert "strategy" in resp.get_json()["error"].lower()

    def test_fallback_must_not_be_in_selector(self, client, auth_headers, admin, seed_outbounds):
        resp = client.post(
            "/api/balancers",
            headers=auth_headers,
            json={
                "tag": "b-fb",
                "selector": ["direct"],
                "fallback_tag": "direct",
            },
        )
        assert resp.status_code == 400
        assert "must not be in selector" in resp.get_json()["error"].lower()

    def test_fallback_must_exist(self, client, auth_headers, admin, seed_outbounds):
        resp = client.post(
            "/api/balancers",
            headers=auth_headers,
            json={
                "tag": "b-fb2",
                "selector": ["direct"],
                "fallback_tag": "no-such-outbound",
            },
        )
        assert resp.status_code == 400
        assert "unknown" in resp.get_json()["error"].lower()


# ===========================================================================
# PUT /api/balancers/<tag>
# ===========================================================================


class TestUpdateBalancer:
    @pytest.fixture(autouse=True)
    def _seed(self, seed_outbounds):
        db.session.add(
            Balancer(
                tag="test-bal",
                selector=json.dumps(["direct"]),
                strategy="random",
                enable=True,
            )
        )
        db.session.commit()

    def test_update_strategy(self, client, auth_headers, admin):
        resp = client.put(
            "/api/balancers/test-bal",
            headers=auth_headers,
            json={"strategy": "leastLoad"},
        )
        assert resp.status_code == 200
        assert Balancer.query.filter_by(tag="test-bal").first().strategy == "leastLoad"

    def test_update_selector(self, client, auth_headers, admin):
        resp = client.put(
            "/api/balancers/test-bal",
            headers=auth_headers,
            json={"selector": ["direct", "proxy-de"]},
        )
        assert resp.status_code == 200
        bal = Balancer.query.filter_by(tag="test-bal").first()
        assert json.loads(bal.selector) == ["direct", "proxy-de"]

    def test_update_enable(self, client, auth_headers, admin):
        resp = client.put(
            "/api/balancers/test-bal",
            headers=auth_headers,
            json={"enable": False},
        )
        assert resp.status_code == 200
        assert Balancer.query.filter_by(tag="test-bal").first().enable is False

    def test_update_fallback(self, client, auth_headers, admin):
        resp = client.put(
            "/api/balancers/test-bal",
            headers=auth_headers,
            json={"fallback_tag": "proxy-de"},
        )
        assert resp.status_code == 200
        assert Balancer.query.filter_by(tag="test-bal").first().fallback_tag == "proxy-de"

    def test_update_nonexistent_returns_404(self, client, auth_headers, admin):
        resp = client.put(
            "/api/balancers/no-such",
            headers=auth_headers,
            json={"strategy": "random"},
        )
        assert resp.status_code == 404

    def test_update_reserved_tag_rejected(self, client, auth_headers, admin):
        db.session.add(
            Balancer(
                tag="system_auto_balancer",
                selector=json.dumps(["direct"]),
                strategy="random",
            )
        )
        db.session.commit()
        resp = client.put(
            "/api/balancers/system_auto_balancer",
            headers=auth_headers,
            json={"strategy": "leastPing"},
        )
        assert resp.status_code == 400
        assert "reserved" in resp.get_json()["error"].lower()

    def test_update_selector_empty_rejected(self, client, auth_headers, admin):
        resp = client.put(
            "/api/balancers/test-bal",
            headers=auth_headers,
            json={"selector": []},
        )
        assert resp.status_code == 400

    def test_update_selector_unknown_tag_rejected(self, client, auth_headers, admin):
        resp = client.put(
            "/api/balancers/test-bal",
            headers=auth_headers,
            json={"selector": ["ghost"]},
        )
        assert resp.status_code == 400
        assert "unknown" in resp.get_json()["error"].lower()

    def test_update_invalid_strategy_rejected(self, client, auth_headers, admin):
        resp = client.put(
            "/api/balancers/test-bal",
            headers=auth_headers,
            json={"strategy": "fastest"},
        )
        assert resp.status_code == 400

    def test_partial_update_keeps_selector(self, client, auth_headers, admin):
        """Updating strategy alone should not clear the selector."""
        client.put(
            "/api/balancers/test-bal",
            headers=auth_headers,
            json={"strategy": "leastPing"},
        )
        bal = Balancer.query.filter_by(tag="test-bal").first()
        assert json.loads(bal.selector) == ["direct"]


# ===========================================================================
# DELETE /api/balancers/<tag>
# ===========================================================================


class TestDeleteBalancer:
    def test_delete_balancer(self, client, auth_headers, admin, seed_outbounds):
        db.session.add(
            Balancer(
                tag="to-delete",
                selector=json.dumps(["direct"]),
                strategy="random",
            )
        )
        db.session.commit()
        resp = client.delete("/api/balancers/to-delete", headers=auth_headers)
        assert resp.status_code == 200
        assert Balancer.query.filter_by(tag="to-delete").first() is None

    def test_delete_nonexistent_returns_404(self, client, auth_headers, admin):
        resp = client.delete("/api/balancers/no-such", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_clears_client_preferred_outbound(self, client, auth_headers, admin, seed_outbounds):
        """Deleting a balancer should null out preferred_outbound on clients that referenced it."""
        db.session.add(
            Balancer(
                tag="bal-del",
                selector=json.dumps(["direct"]),
                strategy="random",
            )
        )
        inbound = Inbound(tag="vless-in", port=10001, protocol="vless", stream_settings="{}")
        db.session.add(inbound)
        db.session.flush()
        c = Client(
            id="uuid-2",
            email="user2",
            inbound_tag="vless-in",
            preferred_outbound="bal-del",
        )
        db.session.add(c)
        db.session.commit()

        resp = client.delete("/api/balancers/bal-del", headers=auth_headers)
        assert resp.status_code == 200
        assert db.session.get(Client, "uuid-2").preferred_outbound is None


# ===========================================================================
# Helper coverage
# ===========================================================================


class TestHelpers:
    """Unit-level coverage for internal helpers via the API surface."""

    def test_parse_bool_truthy_string(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "bool-t", "protocol": "freedom", "enable": "yes"},
        )
        assert resp.status_code == 201
        assert Outbound.query.filter_by(tag="bool-t").first().enable is True

    def test_parse_bool_falsy_string(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "bool-f", "protocol": "freedom", "enable": "off"},
        )
        assert resp.status_code == 201
        assert Outbound.query.filter_by(tag="bool-f").first().enable is False

    def test_protocol_too_long_rejected(self, client, auth_headers, admin):
        resp = client.post(
            "/api/outbounds",
            headers=auth_headers,
            json={"tag": "long-p", "protocol": "x" * 31},
        )
        assert resp.status_code == 400
        assert "too long" in resp.get_json()["error"].lower()
