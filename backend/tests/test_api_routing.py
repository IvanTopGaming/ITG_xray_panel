import datetime
import json
from unittest.mock import patch

import jwt
import pytest

from app.api.routing import _normalize_rules
from app.extensions import db
from app.models import Admin, RoutingProfile, Outbound, Balancer, Inbound
from app.utils import SECRET_KEY


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

    from app.api import routing as routing_api

    if not any(bp_name == "routing" for bp_name in app.blueprints):
        app.register_blueprint(routing_api.bp, url_prefix="/api")
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
def outbound_direct(app):

    ob = Outbound(tag="direct", protocol="freedom", enable=True)
    db.session.add(ob)
    db.session.commit()
    return ob


@pytest.fixture
def outbound_block(app):

    ob = Outbound(tag="block", protocol="blackhole", enable=True)
    db.session.add(ob)
    db.session.commit()
    return ob


class TestAuth:
    def test_get_profiles_requires_token(self, client):
        resp = client.get("/api/routing-profiles")
        assert resp.status_code == 401

    def test_create_profile_requires_token(self, client):
        resp = client.post("/api/routing-profiles", json={"name": "test"})
        assert resp.status_code == 401

    def test_update_profile_requires_token(self, client):
        resp = client.put("/api/routing-profiles/1", json={"name": "test"})
        assert resp.status_code == 401

    def test_delete_profile_requires_token(self, client):
        resp = client.delete("/api/routing-profiles/1")
        assert resp.status_code == 401


class TestGetProfiles:
    def test_empty_list(self, client, auth_headers):
        resp = client.get("/api/routing-profiles", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_all_profiles(self, client, auth_headers):
        p1 = RoutingProfile(name="profile-a", enable=True, rules="[]")
        p2 = RoutingProfile(name="profile-b", enable=False, rules='[{"type":"field"}]')
        db.session.add_all([p1, p2])
        db.session.commit()

        resp = client.get("/api/routing-profiles", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 2
        names = {d["name"] for d in data}
        assert names == {"profile-a", "profile-b"}

    def test_profile_fields(self, client, auth_headers):
        rules = [{"type": "field", "outboundTag": "direct", "domain": ["example.com"]}]
        p = RoutingProfile(name="full", enable=True, rules=json.dumps(rules))
        db.session.add(p)
        db.session.commit()

        resp = client.get("/api/routing-profiles", headers=auth_headers)
        data = resp.get_json()
        assert len(data) == 1
        item = data[0]
        assert item["name"] == "full"
        assert item["enable"] is True
        assert item["rules"] == rules
        assert "id" in item

    def test_malformed_rules_json_returns_empty_list(self, client, auth_headers):

        p = RoutingProfile(name="broken", enable=True, rules="{bad json")
        db.session.add(p)
        db.session.commit()

        resp = client.get("/api/routing-profiles", headers=auth_headers)
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["rules"] == []


class TestCreateProfile:
    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_create_minimal(self, mock_gen, mock_restart, client, auth_headers):
        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={"name": "new-profile"},
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["name"] == "new-profile"
        assert body["enable"] is True
        assert "id" in body

    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_create_with_rules(self, mock_gen, mock_restart, client, auth_headers, outbound_direct):
        rules = [
            {
                "outboundTag": "direct",
                "domain": ["example.com"],
                "enabled": True,
            }
        ]
        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={"name": "with-rules", "rules": rules},
        )
        assert resp.status_code == 201

        p = RoutingProfile.query.filter_by(name="with-rules").first()
        assert p is not None
        stored_rules = json.loads(p.rules)
        assert len(stored_rules) == 1
        assert stored_rules[0]["outboundTag"] == "direct"
        assert stored_rules[0]["domain"] == ["example.com"]

    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_create_disabled(self, mock_gen, mock_restart, client, auth_headers):
        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={"name": "disabled-one", "enable": False},
        )
        assert resp.status_code == 201
        assert resp.get_json()["enable"] is False

    def test_create_missing_name(self, client, auth_headers):
        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={},
        )
        assert resp.status_code == 400
        assert "name" in resp.get_json()["error"].lower()

    def test_create_empty_name(self, client, auth_headers):
        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={"name": "   "},
        )
        assert resp.status_code == 400

    def test_create_name_too_long(self, client, auth_headers):
        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={"name": "a" * 51},
        )
        assert resp.status_code == 400
        assert "too long" in resp.get_json()["error"].lower()

    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_create_duplicate_name(self, mock_gen, mock_restart, client, auth_headers):

        client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={"name": "dup"},
        )

        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={"name": "dup"},
        )
        assert resp.status_code == 400
        assert "exists" in resp.get_json()["error"].lower()

    def test_create_rules_not_array(self, client, auth_headers):
        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={"name": "bad", "rules": "not-a-list"},
        )
        assert resp.status_code == 400
        assert "array" in resp.get_json()["error"].lower()

    def test_create_rule_missing_target(self, client, auth_headers):
        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={"name": "no-target", "rules": [{"domain": ["x.com"]}]},
        )
        assert resp.status_code == 400
        assert "target" in resp.get_json()["error"].lower()

    def test_create_rule_unknown_target(self, client, auth_headers):
        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={
                "name": "bad-target",
                "rules": [{"outboundTag": "nonexistent", "domain": ["x.com"]}],
            },
        )
        assert resp.status_code == 400
        assert "unknown" in resp.get_json()["error"].lower()

    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_create_rule_with_balancer_target(self, mock_gen, mock_restart, client, auth_headers, app):
        bal = Balancer(tag="my-balancer", enable=True)
        db.session.add(bal)
        db.session.commit()

        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={
                "name": "bal-profile",
                "rules": [{"outboundTag": "my-balancer", "domain": ["x.com"]}],
            },
        )
        assert resp.status_code == 201


class TestUpdateProfile:
    @pytest.fixture
    def profile(self, app, outbound_direct):
        rules = [{"type": "field", "outboundTag": "direct", "enabled": True}]
        p = RoutingProfile(name="existing", enable=True, rules=json.dumps(rules))
        db.session.add(p)
        db.session.commit()
        return p

    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_update_name(self, mock_gen, mock_restart, client, auth_headers, profile):
        resp = client.put(
            f"/api/routing-profiles/{profile.id}",
            headers=auth_headers,
            json={"name": "renamed"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "updated"
        mock_gen.assert_called_once()
        mock_restart.assert_called_once()

        db.session.refresh(profile)
        assert profile.name == "renamed"

    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_update_enable(self, mock_gen, mock_restart, client, auth_headers, profile):
        resp = client.put(
            f"/api/routing-profiles/{profile.id}",
            headers=auth_headers,
            json={"enable": False},
        )
        assert resp.status_code == 200

        db.session.refresh(profile)
        assert profile.enable is False

    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_update_rules(self, mock_gen, mock_restart, client, auth_headers, profile, outbound_block):
        new_rules = [{"outboundTag": "block", "domain": ["ads.example.com"], "enabled": True}]
        resp = client.put(
            f"/api/routing-profiles/{profile.id}",
            headers=auth_headers,
            json={"rules": new_rules},
        )
        assert resp.status_code == 200

        db.session.refresh(profile)
        stored = json.loads(profile.rules)
        assert len(stored) == 1
        assert stored[0]["outboundTag"] == "block"

    def test_update_not_found(self, client, auth_headers):
        resp = client.put(
            "/api/routing-profiles/9999",
            headers=auth_headers,
            json={"name": "x"},
        )
        assert resp.status_code == 404

    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_update_duplicate_name(self, mock_gen, mock_restart, client, auth_headers, profile):
        other = RoutingProfile(name="other", enable=True, rules="[]")
        db.session.add(other)
        db.session.commit()

        resp = client.put(
            f"/api/routing-profiles/{profile.id}",
            headers=auth_headers,
            json={"name": "other"},
        )
        assert resp.status_code == 400
        assert "exists" in resp.get_json()["error"].lower()

    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_update_same_name_ok(self, mock_gen, mock_restart, client, auth_headers, profile):

        resp = client.put(
            f"/api/routing-profiles/{profile.id}",
            headers=auth_headers,
            json={"name": "existing"},
        )
        assert resp.status_code == 200

    def test_update_invalid_rules(self, client, auth_headers, profile):
        resp = client.put(
            f"/api/routing-profiles/{profile.id}",
            headers=auth_headers,
            json={"rules": "not-a-list"},
        )
        assert resp.status_code == 400

    def test_update_rule_unknown_target(self, client, auth_headers, profile):
        resp = client.put(
            f"/api/routing-profiles/{profile.id}",
            headers=auth_headers,
            json={"rules": [{"outboundTag": "nonexistent", "domain": ["x.com"]}]},
        )
        assert resp.status_code == 400


class TestDeleteProfile:
    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_delete_profile(self, mock_gen, mock_restart, client, auth_headers):
        p = RoutingProfile(name="to-delete", enable=True, rules="[]")
        db.session.add(p)
        db.session.commit()
        pid = p.id

        resp = client.delete(f"/api/routing-profiles/{pid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "deleted"
        mock_gen.assert_called_once()
        mock_restart.assert_called_once()

        assert db.session.get(RoutingProfile, pid) is None

    def test_delete_not_found(self, client, auth_headers):
        resp = client.delete("/api/routing-profiles/9999", headers=auth_headers)
        assert resp.status_code == 404

    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_delete_unlinks_inbounds(self, mock_gen, mock_restart, client, auth_headers):

        p = RoutingProfile(name="linked", enable=True, rules="[]")
        db.session.add(p)
        db.session.flush()

        ib = Inbound(
            tag="vless-test",
            port=10000,
            protocol="vless",
            stream_settings="{}",
            routing_profile_id=p.id,
        )
        db.session.add(ib)
        db.session.commit()
        pid = p.id
        ib_id = ib.id

        resp = client.delete(f"/api/routing-profiles/{pid}", headers=auth_headers)
        assert resp.status_code == 200

        refreshed_ib = db.session.get(Inbound, ib_id)
        assert refreshed_ib is not None
        assert refreshed_ib.routing_profile_id is None


class TestNormalization:
    def test_parse_bool_truthy(self):
        from app.api.routing import _parse_bool

        for val in [True, 1, "1", "true", "True", "yes", "on"]:
            assert _parse_bool(val) is True

    def test_parse_bool_falsy(self):
        from app.api.routing import _parse_bool

        for val in [False, 0, "0", "false", "False", "no", "off"]:
            assert _parse_bool(val) is False

    def test_parse_bool_default(self):
        from app.api.routing import _parse_bool

        assert _parse_bool(None, default=False) is False
        assert _parse_bool(None, default=True) is True
        assert _parse_bool("garbage", default=False) is False

    def test_normalize_rules_adds_type_field(self):
        from app.api.routing import _normalize_rules

        rules = _normalize_rules([{"outboundTag": "direct"}])
        assert rules[0]["type"] == "field"

    def test_normalize_rules_strips_empty_lists(self):
        from app.api.routing import _normalize_rules

        rules = _normalize_rules([{"outboundTag": "direct", "domain": [], "ip": []}])
        assert "domain" not in rules[0]
        assert "ip" not in rules[0]

    def test_normalize_rules_rejects_non_dict_rule(self):
        from app.api.routing import _normalize_rules

        with pytest.raises(ValueError, match="must be an object"):
            _normalize_rules(["not-a-dict"])

    def test_normalize_rules_accepts_balancerTag(self):
        from app.api.routing import _normalize_rules

        rules = _normalize_rules([{"balancerTag": "my-bal"}])

        assert rules[0]["outboundTag"] == "my-bal"


class TestRuleFieldPrefixes:
    def test_geoip_in_domain_raises(self):
        with pytest.raises(ValueError, match="IPS field"):
            _normalize_rules([{"domain": ["geoip:cn"], "outboundTag": "direct"}])

    def test_geoip_in_domain_case_insensitive(self):
        with pytest.raises(ValueError, match="IPS field"):
            _normalize_rules([{"domain": ["GeoIP:CN"], "outboundTag": "direct"}])

    def test_geosite_in_ip_raises(self):
        with pytest.raises(ValueError, match="Domains field"):
            _normalize_rules([{"ip": ["geosite:google"], "outboundTag": "direct"}])

    def test_domain_matcher_in_source_raises(self):
        with pytest.raises(ValueError, match="Domains field"):
            _normalize_rules([{"source": ["regexp:.*"], "outboundTag": "direct"}])

    def test_all_domain_prefixes_rejected_in_ip(self):
        for prefix in ("geosite:", "domain:", "regexp:", "keyword:", "full:"):
            with pytest.raises(ValueError, match="Domains field"):
                _normalize_rules([{"ip": [f"{prefix}x"], "outboundTag": "direct"}])

    def test_valid_prefixes_pass(self):
        rules = _normalize_rules(
            [
                {
                    "domain": ["geosite:google", "example.com"],
                    "ip": ["geoip:cn", "8.8.8.8", "ext:geoip.dat:cn"],
                    "source": ["10.0.0.0/24"],
                    "outboundTag": "direct",
                }
            ]
        )
        assert rules[0]["domain"] == ["geosite:google", "example.com"]
        assert rules[0]["ip"] == ["geoip:cn", "8.8.8.8", "ext:geoip.dat:cn"]

    def test_unknown_protocol_rejected(self):
        with pytest.raises(ValueError, match="unknown protocol"):
            _normalize_rules([{"protocol": ["randomxyz"], "outboundTag": "direct"}])

    def test_valid_protocols_pass(self):
        rules = _normalize_rules([{"protocol": ["http", "TLS", "quic", "bittorrent"], "outboundTag": "direct"}])
        assert rules[0]["protocol"] == ["http", "TLS", "quic", "bittorrent"]

    def test_dotless_domain_matcher_rejected_in_ip(self):
        with pytest.raises(ValueError, match="Domains field"):
            _normalize_rules([{"ip": ["dotless:x"], "outboundTag": "direct"}])

    def test_negated_domain_matcher_rejected_in_ip(self):
        with pytest.raises(ValueError, match="Domains field"):
            _normalize_rules([{"ip": ["!geosite:google"], "outboundTag": "direct"}])

    def test_negated_geoip_in_ip_passes(self):
        rules = _normalize_rules([{"ip": ["!geoip:cn"], "outboundTag": "direct"}])
        assert rules[0]["ip"] == ["!geoip:cn"]

    def test_unknown_network_rejected(self):
        with pytest.raises(ValueError, match="unknown network"):
            _normalize_rules([{"network": "tcp,randomnet", "outboundTag": "direct"}])

    def test_valid_networks_pass(self):
        rules = _normalize_rules([{"network": "TCP,udp", "outboundTag": "direct"}])
        assert rules[0]["network"] == "TCP,udp"

    @patch("app.api.routing.restart_xray_container")
    @patch("app.api.routing.generate_config_file")
    def test_api_create_rejects_geoip_in_domain(self, mock_gen, mock_restart, client, auth_headers):
        resp = client.post(
            "/api/routing-profiles",
            headers=auth_headers,
            json={"name": "p-bad", "rules": [{"domain": ["geoip:cn"], "outboundTag": "direct"}]},
        )
        assert resp.status_code == 400
        assert "IPS" in resp.get_json()["error"]
