"""Tests for app.services.xray — pure logic + config generation.

Covers _build_stream_settings, generate_config_file, get_system_settings,
and key stripping behaviour.
"""

import json
import uuid

import pytest
from unittest.mock import patch

from app.extensions import db
from app.models import Client, Inbound, Outbound, RoutingProfile, SystemSetting


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reality_keys():
    """Return a deterministic REALITY key pair (valid x25519)."""
    from cryptography.hazmat.primitives.asymmetric import x25519
    from cryptography.hazmat.primitives import serialization
    import base64

    priv = x25519.X25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pk = base64.urlsafe_b64encode(priv_bytes).decode().rstrip("=")
    pub = base64.urlsafe_b64encode(pub_bytes).decode().rstrip("=")
    return pk, pub


# ---------------------------------------------------------------------------
# _build_stream_settings
# ---------------------------------------------------------------------------


class TestBuildStreamSettings:
    """Unit tests for _build_stream_settings (no DB needed)."""

    def test_vless_reality(self):
        from app.services.xray import _build_stream_settings

        pk, pub = _reality_keys()
        inp = {
            "protocol": "vless",
            "network": "tcp",
            "security": "reality",
            "realityPrivateKey": pk,
            "realityPublicKey": pub,
            "realitySNI": "www.example.com",
            "realityDest": "www.example.com:443",
            "realityShortIds": "abcdef01",
            "realityFingerprint": "chrome",
            "realitySpiderX": "/",
        }
        result = _build_stream_settings(inp)

        assert result["network"] == "tcp"
        assert result["security"] == "reality"
        rs = result["realitySettings"]
        assert rs["serverNames"] == ["www.example.com"]
        assert rs["dest"] == "www.example.com:443"
        assert rs["shortIds"] == ["abcdef01"]
        assert rs["fingerprint"] == "chrome"
        assert rs["spiderX"] == "/"
        # Keys should be normalised but present
        assert rs["privateKey"]
        assert rs["publicKey"]

    def test_websocket_transport(self):
        from app.services.xray import _build_stream_settings

        inp = {
            "protocol": "vless",
            "network": "ws",
            "security": "none",
            "wsPath": "/my-ws",
            "wsHost": "cdn.example.com",
        }
        result = _build_stream_settings(inp)

        assert result["network"] == "ws"
        ws = result["wsSettings"]
        assert ws["path"] == "/my-ws"
        assert ws["headers"]["Host"] == "cdn.example.com"

    def test_websocket_path_slash_prefix(self):
        from app.services.xray import _build_stream_settings

        inp = {
            "protocol": "vless",
            "network": "ws",
            "security": "none",
            "wsPath": "no-slash",
        }
        result = _build_stream_settings(inp)
        assert result["wsSettings"]["path"] == "/no-slash"

    def test_shadowsocks_extra_keys_preserved(self):
        """ssMethod, ssPassword, ssNetwork should appear in the returned stream dict."""
        import base64
        import secrets
        from app.services.xray import _build_stream_settings

        method = "2022-blake3-aes-128-gcm"
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        inp = {
            "protocol": "shadowsocks",
            "network": "tcp",
            "security": "none",
            "ssMethod": method,
            "ssPassword": key,
            "ssNetwork": "tcp,udp",
        }
        result = _build_stream_settings(inp)

        assert result["ssMethod"] == method
        assert result["ssPassword"]  # normalised, non-empty
        assert result["ssNetwork"] == "tcp,udp"

    def test_grpc_transport(self):
        from app.services.xray import _build_stream_settings

        inp = {
            "protocol": "vless",
            "network": "grpc",
            "security": "none",
            "grpcServiceName": "my-grpc",
        }
        result = _build_stream_settings(inp)
        assert result["network"] == "grpc"
        assert result["grpcSettings"]["serviceName"] == "my-grpc"

    def test_socks_auth_keys(self):
        from app.services.xray import _build_stream_settings

        inp = {
            "protocol": "socks",
            "network": "tcp",
            "security": "none",
            "authUser": "user1",
            "authPass": "pass1",
        }
        result = _build_stream_settings(inp)
        assert result["authUser"] == "user1"
        assert result["authPass"] == "pass1"
        # SOCKS forces tcp + none
        assert result["network"] == "tcp"
        assert result["security"] == "none"

    def test_socks_auth_partial_raises(self):
        from app.services.xray import _build_stream_settings

        inp = {
            "protocol": "socks",
            "network": "tcp",
            "security": "none",
            "authUser": "user1",
            "authPass": "",
        }
        with pytest.raises(ValueError, match="together"):
            _build_stream_settings(inp)

    def test_reality_invalid_private_key_raises(self):
        from app.services.xray import _build_stream_settings

        inp = {
            "protocol": "vless",
            "network": "tcp",
            "security": "reality",
            "realityPrivateKey": "not-a-valid-key",
        }
        with pytest.raises(ValueError, match="REALITY private key"):
            _build_stream_settings(inp)

    def test_tls_settings(self):
        from app.services.xray import _build_stream_settings

        inp = {
            "protocol": "vless",
            "network": "tcp",
            "security": "tls",
            "tlsServerName": "example.com",
            "tlsAlpn": "h2,http/1.1",
            "tlsCertFile": "/certs/cert.pem",
            "tlsKeyFile": "/certs/key.pem",
        }
        result = _build_stream_settings(inp)
        assert result["security"] == "tls"
        tls = result["tlsSettings"]
        assert tls["serverName"] == "example.com"
        assert tls["alpn"] == ["h2", "http/1.1"]
        assert tls["certificates"][0]["certificateFile"] == "/certs/cert.pem"

    def test_httpupgrade_transport(self):
        from app.services.xray import _build_stream_settings

        inp = {
            "protocol": "vless",
            "network": "httpupgrade",
            "security": "none",
            "wsPath": "/upgrade",
            "wsHost": "host.example.com",
        }
        result = _build_stream_settings(inp)
        assert result["network"] == "httpupgrade"
        assert result["httpUpgradeSettings"]["path"] == "/upgrade"
        assert result["httpUpgradeSettings"]["host"] == "host.example.com"


# ---------------------------------------------------------------------------
# generate_config_file  (requires DB fixtures)
# ---------------------------------------------------------------------------


class TestGenerateConfigFile:
    """Integration tests — seed DB rows, generate config, parse the JSON."""

    @pytest.fixture(autouse=True)
    def _setup(self, app, db, tmp_path):
        """Provide app context and redirect file I/O to tmp_path."""
        self.app = app
        self.db = db
        self.tmp_path = tmp_path
        self._patches = [
            patch("app.services.xray.LOCK_PATH", str(tmp_path / "config.lock")),
            patch("app.services.xray.CONFIG_PATH", str(tmp_path / "config.json")),
            patch("app.services.xray.restart_xray_container"),
        ]
        for p in self._patches:
            p.start()
        yield
        for p in self._patches:
            p.stop()

    def _seed_outbounds(self):
        """Create the mandatory direct + block outbounds."""
        db.session.add(
            Outbound(
                tag="direct",
                protocol="freedom",
                enable=True,
                settings="{}",
                stream_settings="{}",
                mux="{}",
            )
        )
        db.session.add(
            Outbound(
                tag="block",
                protocol="blackhole",
                enable=True,
                settings=json.dumps({"response": {"type": "none"}}),
                stream_settings="{}",
                mux="{}",
            )
        )
        db.session.commit()

    def _read_config(self):
        with open(str(self.tmp_path / "config.json"), "r") as f:
            return json.load(f)

    # ---- test: basic VLESS inbound with a client ----

    def test_generates_valid_json_with_inbound_and_client(self):
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        pk, pub = _reality_keys()
        stream = json.dumps(
            {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": "www.google.com:443",
                    "xver": 0,
                    "serverNames": ["www.google.com"],
                    "privateKey": pk,
                    "publicKey": pub,
                    "shortIds": ["abcdef01"],
                    "fingerprint": "chrome",
                    "spiderX": "",
                },
            }
        )

        ib = Inbound(tag="vless-in", port=443, protocol="vless", stream_settings=stream)
        db.session.add(ib)
        db.session.flush()

        client_uuid = str(uuid.uuid4())
        c = Client(
            id=client_uuid,
            email="alice",
            inbound_tag="vless-in",
            enable=True,
            flow="xtls-rprx-vision",
        )
        db.session.add(c)
        db.session.commit()

        generate_config_file()

        cfg = self._read_config()

        # Top-level structure
        assert "inbounds" in cfg
        assert "outbounds" in cfg
        assert "routing" in cfg
        assert "log" in cfg
        assert "stats" in cfg

        # Find our inbound (skip the api dokodemo-door)
        vless_inbounds = [i for i in cfg["inbounds"] if i["tag"] == "vless-in"]
        assert len(vless_inbounds) == 1
        vless_ib = vless_inbounds[0]
        assert vless_ib["port"] == 443
        assert vless_ib["protocol"] == "vless"

        # Client should be in settings
        clients = vless_ib["settings"]["clients"]
        assert len(clients) == 1
        assert clients[0]["id"] == client_uuid
        assert clients[0]["flow"] == "xtls-rprx-vision"

    # ---- test: UI-only keys are stripped ----

    def test_strips_ui_only_keys(self):
        """ssMethod, ssPassword, ssNetwork, authUser, authPass, wg* must NOT appear
        in the written Xray config's streamSettings."""
        import base64
        import secrets
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        method = "2022-blake3-aes-128-gcm"
        server_key = base64.b64encode(secrets.token_bytes(16)).decode()
        stream = json.dumps(
            {
                "network": "tcp",
                "security": "none",
                "ssMethod": method,
                "ssPassword": server_key,
                "ssNetwork": "tcp,udp",
            }
        )

        ib = Inbound(tag="ss-in", port=8388, protocol="shadowsocks", stream_settings=stream)
        db.session.add(ib)
        db.session.flush()

        client_key = base64.b64encode(secrets.token_bytes(16)).decode()
        c = Client(id=client_key, email="bob", inbound_tag="ss-in", enable=True)
        db.session.add(c)
        db.session.commit()

        generate_config_file()
        cfg = self._read_config()

        ss_ib = [i for i in cfg["inbounds"] if i["tag"] == "ss-in"][0]
        ss_stream = ss_ib.get("streamSettings", {})

        extra_keys = {
            "ssMethod",
            "ssPassword",
            "ssNetwork",
            "authUser",
            "authPass",
            "wgSecretKey",
            "wgPublicKey",
            "wgMTU",
        }
        found = extra_keys & set(ss_stream.keys())
        assert not found, f"UI-only keys leaked into config: {found}"

        # But the protocol settings should still have the method + password
        assert ss_ib["settings"]["method"] == method
        assert ss_ib["settings"]["password"]

    # ---- test: enabled vs disabled clients ----

    def test_includes_enabled_excludes_disabled(self):
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        stream = json.dumps({"network": "tcp", "security": "none"})
        ib = Inbound(tag="vless-filter", port=10443, protocol="vless", stream_settings=stream)
        db.session.add(ib)
        db.session.flush()

        enabled_uuid = str(uuid.uuid4())
        disabled_uuid = str(uuid.uuid4())
        db.session.add(
            Client(
                id=enabled_uuid,
                email="enabled-user",
                inbound_tag="vless-filter",
                enable=True,
            )
        )
        db.session.add(
            Client(
                id=disabled_uuid,
                email="disabled-user",
                inbound_tag="vless-filter",
                enable=False,
            )
        )
        db.session.commit()

        generate_config_file()
        cfg = self._read_config()

        vless_ib = [i for i in cfg["inbounds"] if i["tag"] == "vless-filter"][0]
        client_ids = [c["id"] for c in vless_ib["settings"]["clients"]]
        assert enabled_uuid in client_ids
        assert disabled_uuid not in client_ids

    # ---- test: trojan protocol ----

    def test_trojan_inbound(self):
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        stream = json.dumps({"network": "tcp", "security": "none"})
        ib = Inbound(tag="trojan-in", port=9443, protocol="trojan", stream_settings=stream)
        db.session.add(ib)
        db.session.flush()

        client_pw = str(uuid.uuid4())
        db.session.add(
            Client(
                id=client_pw,
                email="trojan-user",
                inbound_tag="trojan-in",
                enable=True,
            )
        )
        db.session.commit()

        generate_config_file()
        cfg = self._read_config()

        trojan_ib = [i for i in cfg["inbounds"] if i["tag"] == "trojan-in"][0]
        clients = trojan_ib["settings"]["clients"]
        assert len(clients) == 1
        assert clients[0]["password"] == client_pw

    # ---- test: socks inbound with auth ----

    def test_socks_inbound_with_auth(self):
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        stream = json.dumps(
            {
                "network": "tcp",
                "security": "none",
                "authUser": "myuser",
                "authPass": "mypass",
            }
        )
        ib = Inbound(tag="socks-in", port=1080, protocol="socks", stream_settings=stream)
        db.session.add(ib)
        db.session.commit()

        generate_config_file()
        cfg = self._read_config()

        socks_ib = [i for i in cfg["inbounds"] if i["tag"] == "socks-in"][0]
        assert socks_ib["settings"]["auth"] == "password"
        assert socks_ib["settings"]["accounts"][0]["user"] == "myuser"
        assert socks_ib["settings"]["accounts"][0]["pass"] == "mypass"

        # authUser/authPass must be stripped from streamSettings
        ss = socks_ib.get("streamSettings", {})
        assert "authUser" not in ss
        assert "authPass" not in ss

    # ---- test: routing profile attached to inbound ----

    def test_routing_profile_rules_included(self):
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        profile = RoutingProfile(
            name="block-ads",
            enable=True,
            rules=json.dumps(
                [
                    {
                        "type": "field",
                        "domain": ["geosite:category-ads"],
                        "outboundTag": "block",
                        "enabled": True,
                    }
                ]
            ),
        )
        db.session.add(profile)
        db.session.flush()

        stream = json.dumps({"network": "tcp", "security": "none"})
        ib = Inbound(
            tag="vless-routed",
            port=11443,
            protocol="vless",
            stream_settings=stream,
            routing_profile_id=profile.id,
        )
        db.session.add(ib)
        db.session.commit()

        generate_config_file()
        cfg = self._read_config()

        rules = cfg["routing"]["rules"]
        # Find a rule with domain matching
        domain_rules = [r for r in rules if "domain" in r]
        assert len(domain_rules) >= 1
        assert "geosite:category-ads" in domain_rules[0]["domain"]
        assert domain_rules[0]["outboundTag"] == "block"

    # ---- test: a proxy outbound is inert until explicitly routed ----

    def test_proxy_outbound_does_not_create_implicit_catch_all(self):
        """A proxy outbound must not silently capture all traffic.

        Two inbounds, one proxy outbound. The first inbound has a routing
        profile sending its traffic to the proxy outbound; the second inbound
        has no rules. The second inbound's traffic must fall through to the
        first outbound (``direct``) — there must be NO implicit
        ``system_auto_balancer`` and NO unfiltered tcp/udp catch-all rule.
        """
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        # A proxy (non-system) outbound.
        db.session.add(
            Outbound(
                tag="proxy-out",
                protocol="vless",
                enable=True,
                settings="{}",
                stream_settings="{}",
                mux="{}",
            )
        )

        # Inbound #1: routed to the proxy outbound via a routing profile.
        profile = RoutingProfile(
            name="via-proxy",
            enable=True,
            rules=json.dumps([{"type": "field", "outboundTag": "proxy-out", "enabled": True}]),
        )
        db.session.add(profile)
        db.session.flush()

        stream = json.dumps({"network": "tcp", "security": "none"})
        db.session.add(
            Inbound(
                tag="in-routed",
                port=12443,
                protocol="vless",
                stream_settings=stream,
                routing_profile_id=profile.id,
            )
        )

        # Inbound #2: no routing profile — implies direct egress.
        db.session.add(
            Inbound(
                tag="in-direct",
                port=12444,
                protocol="vless",
                stream_settings=stream,
            )
        )
        db.session.commit()

        generate_config_file()
        cfg = self._read_config()

        balancers = cfg["routing"]["balancers"]
        rules = cfg["routing"]["rules"]

        # No implicit balancer was synthesized.
        assert all(b["tag"] != "system_auto_balancer" for b in balancers)
        # No unfiltered tcp/udp catch-all rule pointing at a balancer.
        catch_all = [r for r in rules if r.get("network") == "tcp,udp" and "balancerTag" in r and "inboundTag" not in r]
        assert catch_all == []

        # direct is the first outbound, so unmatched (inbound #2) traffic egresses direct.
        assert cfg["outbounds"][0]["tag"] == "direct"
        assert "proxy-out" in [o["tag"] for o in cfg["outbounds"]]

        # Sanity: inbound #1's rule still targets the proxy outbound, scoped to itself.
        routed = [r for r in rules if r.get("outboundTag") == "proxy-out"]
        assert len(routed) == 1
        assert "in-routed" in routed[0]["inboundTag"]
        assert "in-direct" not in routed[0].get("inboundTag", [])

    # ---- test: port 443 gets sockopt ----

    def test_port_443_gets_sockopt(self):
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        stream = json.dumps({"network": "tcp", "security": "none"})
        ib = Inbound(tag="vless-443", port=443, protocol="vless", stream_settings=stream)
        db.session.add(ib)
        db.session.commit()

        generate_config_file()
        cfg = self._read_config()

        vless_ib = [i for i in cfg["inbounds"] if i["tag"] == "vless-443"][0]
        sockopt = vless_ib["streamSettings"]["sockopt"]
        assert sockopt["acceptProxyProtocol"] is True

    # ---- test: empty DB produces minimal valid config ----

    def test_empty_db_produces_valid_config(self):
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        generate_config_file()
        cfg = self._read_config()

        assert cfg["inbounds"][0]["tag"] == "api"  # dokodemo-door api inbound
        assert "outbounds" in cfg
        assert cfg["routing"]["rules"][0]["inboundTag"] == ["api"]

    # ---- test: http inbound no auth ----

    def test_http_inbound_no_auth(self):
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        stream = json.dumps({"network": "tcp", "security": "none"})
        ib = Inbound(tag="http-in", port=8080, protocol="http", stream_settings=stream)
        db.session.add(ib)
        db.session.commit()

        generate_config_file()
        cfg = self._read_config()

        http_ib = [i for i in cfg["inbounds"] if i["tag"] == "http-in"][0]
        assert http_ib["settings"]["allowTransparent"] is False
        assert "accounts" not in http_ib["settings"]


# ---------------------------------------------------------------------------
# get_system_settings
# ---------------------------------------------------------------------------


class TestGetSystemSettings:
    """Tests for get_system_settings — defaults and DB overrides."""

    def test_returns_defaults_when_no_rows(self, app, db):
        from app.services.xray import get_system_settings

        result = get_system_settings()

        assert result["xrayLogLevel"] in {"debug", "info", "warning", "error", "none"}
        assert result["geoipUrl"].startswith("http")
        assert result["geositeUrl"].startswith("http")

    def test_reads_overrides_from_db(self, app, db):
        from app.services.xray import get_system_settings

        db.session.add(SystemSetting(key="xray_log_level", value="warning"))
        db.session.add(SystemSetting(key="geoip_url", value="https://custom.example.com/geoip.dat"))
        db.session.add(SystemSetting(key="geosite_url", value="https://custom.example.com/geosite.dat"))
        db.session.commit()

        result = get_system_settings()

        assert result["xrayLogLevel"] == "warning"
        assert result["geoipUrl"] == "https://custom.example.com/geoip.dat"
        assert result["geositeUrl"] == "https://custom.example.com/geosite.dat"

    def test_invalid_log_level_falls_back(self, app, db):
        from app.services.xray import get_system_settings, DEFAULT_LOG_LEVEL

        db.session.add(SystemSetting(key="xray_log_level", value="bogus"))
        db.session.commit()

        result = get_system_settings()
        assert result["xrayLogLevel"] == DEFAULT_LOG_LEVEL

    def test_invalid_url_falls_back(self, app, db):
        from app.services.xray import get_system_settings, DEFAULT_GEOIP_URL

        db.session.add(SystemSetting(key="geoip_url", value="not-a-url"))
        db.session.commit()

        result = get_system_settings()
        # Should fall back to the default, which is a valid URL
        assert result["geoipUrl"] == DEFAULT_GEOIP_URL


# ---------------------------------------------------------------------------
# _derive_reality_pubkey
# ---------------------------------------------------------------------------


class TestDeriveRealityPubkey:
    def test_derives_matching_public_key(self):
        from app.services.xray import _derive_reality_pubkey

        pk, expected_pub = _reality_keys()
        derived = _derive_reality_pubkey(pk)
        assert derived == expected_pub

    def test_returns_empty_for_invalid_key(self):
        from app.services.xray import _derive_reality_pubkey

        assert _derive_reality_pubkey("garbage") == ""
        assert _derive_reality_pubkey("") == ""
        assert _derive_reality_pubkey(None) == ""


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


class TestNormalisationHelpers:
    def test_normalize_xray_log_level_valid(self):
        from app.services.xray import normalize_xray_log_level

        for level in ("debug", "info", "warning", "error", "none"):
            assert normalize_xray_log_level(level) == level

    def test_normalize_xray_log_level_invalid(self):
        from app.services.xray import normalize_xray_log_level

        with pytest.raises(ValueError, match="Invalid Xray log level"):
            normalize_xray_log_level("trace")

    def test_normalize_stream_network(self):
        from app.services.xray import normalize_stream_network

        assert normalize_stream_network("ws") == "ws"
        assert normalize_stream_network("GRPC") == "grpc"
        assert normalize_stream_network("invalid") == "tcp"
        assert normalize_stream_network("") == "tcp"

    def test_normalize_packet_network(self):
        from app.services.xray import normalize_packet_network

        assert normalize_packet_network("tcp") == "tcp"
        assert normalize_packet_network("udp,tcp") == "tcp,udp"  # alias
        assert normalize_packet_network("invalid") == "tcp"

    def test_flag_enabled(self):
        from app.services.xray import _flag_enabled

        assert _flag_enabled(True) is True
        assert _flag_enabled(False) is False
        assert _flag_enabled("yes") is True
        assert _flag_enabled("0") is False
        assert _flag_enabled(None) is True  # default=True
        assert _flag_enabled(None, default=False) is False
