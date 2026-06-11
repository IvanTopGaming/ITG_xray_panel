import json
import uuid

import pytest
from unittest.mock import patch

from app.extensions import db
from app.models import Client, Inbound, Outbound, RoutingProfile, SystemSetting


def _reality_keys():

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


class TestBuildStreamSettings:
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

        assert rs["privateKey"]
        assert rs["publicKey"]

    def test_invalid_reality_fingerprint_rejected(self):
        import pytest
        from app.services.xray import _build_stream_settings

        pk, pub = _reality_keys()
        inp = {
            "protocol": "vless",
            "network": "tcp",
            "security": "reality",
            "realityPrivateKey": pk,
            "realityPublicKey": pub,
            "realityFingerprint": "fake-fp-999",
        }
        with pytest.raises(ValueError, match="REALITY fingerprint"):
            _build_stream_settings(inp)

    def test_invalid_alpn_rejected(self):
        import pytest
        from app.services.xray import _build_stream_settings

        with pytest.raises(ValueError, match="Invalid ALPN"):
            _build_stream_settings({"protocol": "vless", "network": "tcp", "security": "tls", "tlsAlpn": "bogusalpn"})

    def test_valid_alpn_accepted(self):
        from app.services.xray import _build_stream_settings

        result = _build_stream_settings(
            {"protocol": "vless", "network": "tcp", "security": "tls", "tlsAlpn": "h2,http/1.1"}
        )
        assert result["tlsSettings"]["alpn"] == ["h2", "http/1.1"]

    def test_invalid_utls_fingerprint_rejected(self):
        import pytest
        from app.services.xray import _build_stream_settings

        with pytest.raises(ValueError, match="TLS fingerprint"):
            _build_stream_settings(
                {
                    "protocol": "vless",
                    "network": "tcp",
                    "security": "tls",
                    "tlsUTLSFingerprint": "bogus-fp",
                }
            )

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
        assert result["ssPassword"]
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
            "tlsCertFile": "/etc/xray/certs/cert.pem",
            "tlsKeyFile": "/etc/xray/certs/key.pem",
        }
        result = _build_stream_settings(inp)
        assert result["security"] == "tls"
        tls = result["tlsSettings"]
        assert tls["serverName"] == "example.com"
        assert tls["alpn"] == ["h2", "http/1.1"]
        assert tls["certificates"][0]["certificateFile"] == "/etc/xray/certs/cert.pem"

    def test_tls_cert_path_outside_etc_xray_rejected(self):
        from app.services.xray import _build_stream_settings

        inp = {
            "protocol": "vless",
            "network": "tcp",
            "security": "tls",
            "tlsCertFile": "/root/cert/key.pem",
            "tlsKeyFile": "/root/cert/key.pem",
        }
        with pytest.raises(ValueError):
            _build_stream_settings(inp)

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


class TestGenerateConfigFile:
    @pytest.fixture(autouse=True)
    def _setup(self, app, db, tmp_path):

        self.app = app
        self.db = db
        self.tmp_path = tmp_path
        self._patches = [
            patch("app.services.xray.LOCK_PATH", str(tmp_path / "config.lock")),
            patch("app.services.xray.CONFIG_PATH", str(tmp_path / "config.json")),
            patch("app.services.xray.CANDIDATE_PATH", str(tmp_path / "config.json.candidate")),
            patch("app.services.xray._validate_xray_config"),
            patch("app.services.xray.restart_xray_container"),
        ]
        for p in self._patches:
            p.start()
        yield
        for p in self._patches:
            p.stop()

    def _seed_outbounds(self):

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

        assert "inbounds" in cfg
        assert "outbounds" in cfg
        assert "routing" in cfg
        assert "log" in cfg
        assert "stats" in cfg

        vless_inbounds = [i for i in cfg["inbounds"] if i["tag"] == "vless-in"]
        assert len(vless_inbounds) == 1
        vless_ib = vless_inbounds[0]
        assert vless_ib["port"] == 443
        assert vless_ib["protocol"] == "vless"

        clients = vless_ib["settings"]["clients"]
        assert len(clients) == 1
        assert clients[0]["id"] == client_uuid
        assert clients[0]["flow"] == "xtls-rprx-vision"

    def test_logs_regeneration_duration(self, caplog):
        import logging as _logging

        from app.services.xray import generate_config_file

        self._seed_outbounds()

        with caplog.at_level(_logging.INFO, logger="app.services.xray"):
            generate_config_file()

        msgs = [r.getMessage() for r in caplog.records if r.name == "app.services.xray"]
        assert any("config regenerated" in m and "ms" in m for m in msgs)

    def test_clears_incompatible_flow_in_generated_config(self):
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        stream = json.dumps({"network": "xhttp", "security": "none"})
        ib = Inbound(tag="vless-xh", port=8443, protocol="vless", stream_settings=stream)
        db.session.add(ib)
        db.session.flush()

        c = Client(
            id=str(uuid.uuid4()),
            email="legacy",
            inbound_tag="vless-xh",
            enable=True,
            flow="xtls-rprx-vision",
        )
        db.session.add(c)
        db.session.commit()

        generate_config_file()
        cfg = self._read_config()

        xh_ib = [i for i in cfg["inbounds"] if i["tag"] == "vless-xh"][0]
        assert xh_ib["settings"]["clients"][0]["flow"] == ""

    def test_strips_ui_only_keys(self):

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

        assert ss_ib["settings"]["method"] == method
        assert ss_ib["settings"]["password"]

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

        ss = socks_ib.get("streamSettings", {})
        assert "authUser" not in ss
        assert "authPass" not in ss

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

        domain_rules = [r for r in rules if "domain" in r]
        assert len(domain_rules) >= 1
        assert "geosite:category-ads" in domain_rules[0]["domain"]
        assert domain_rules[0]["outboundTag"] == "block"

    def test_proxy_outbound_does_not_create_implicit_catch_all(self):

        from app.services.xray import generate_config_file

        self._seed_outbounds()

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

        assert all(b["tag"] != "system_auto_balancer" for b in balancers)

        catch_all = [r for r in rules if r.get("network") == "tcp,udp" and "balancerTag" in r and "inboundTag" not in r]
        assert catch_all == []

        assert cfg["outbounds"][0]["tag"] == "direct"
        assert "proxy-out" in [o["tag"] for o in cfg["outbounds"]]

        routed = [r for r in rules if r.get("outboundTag") == "proxy-out"]
        assert len(routed) == 1
        assert "in-routed" in routed[0]["inboundTag"]
        assert "in-direct" not in routed[0].get("inboundTag", [])

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

    def test_empty_db_produces_valid_config(self):
        from app.services.xray import generate_config_file

        self._seed_outbounds()

        generate_config_file()
        cfg = self._read_config()

        assert cfg["inbounds"][0]["tag"] == "api"
        assert "outbounds" in cfg
        assert cfg["routing"]["rules"][0]["inboundTag"] == ["api"]

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


class TestGetSystemSettings:
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

        assert result["geoipUrl"] == DEFAULT_GEOIP_URL


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
        assert normalize_packet_network("udp,tcp") == "tcp,udp"
        assert normalize_packet_network("invalid") == "tcp"

    def test_flag_enabled(self):
        from app.services.xray import _flag_enabled

        assert _flag_enabled(True) is True
        assert _flag_enabled(False) is False
        assert _flag_enabled("yes") is True
        assert _flag_enabled("0") is False
        assert _flag_enabled(None) is True
        assert _flag_enabled(None, default=False) is False


class TestValidateXrayConfig:
    def test_skips_when_binary_absent(self):
        from app.services import xray

        with (
            patch("app.services.xray.os.path.exists", return_value=False),
            patch("app.services.xray.subprocess.run") as run,
        ):
            xray._validate_xray_config("/tmp/cfg.json")
            run.assert_not_called()

    def test_accepts_valid_config(self):
        from unittest.mock import MagicMock

        from app.services import xray

        ok = MagicMock(returncode=0, stderr=b"", stdout=b"Configuration OK.")
        with (
            patch("app.services.xray.os.path.exists", return_value=True),
            patch("app.services.xray.subprocess.run", return_value=ok) as run,
        ):
            xray._validate_xray_config("/tmp/cfg.json")
            assert run.call_count == 1

    def test_rejects_bad_config_fail_closed(self):
        from unittest.mock import MagicMock

        from app.services import xray

        bad = MagicMock(returncode=1, stderr=b"infra/conf: something broke", stdout=b"")
        with (
            patch("app.services.xray.os.path.exists", return_value=True),
            patch("app.services.xray.subprocess.run", return_value=bad),
        ):
            with pytest.raises(ValueError, match="Xray rejected the config"):
                xray._validate_xray_config("/tmp/cfg.json")

    def test_retries_once_on_transient_timeout(self):
        import subprocess as sp
        from unittest.mock import MagicMock

        from app.services import xray

        ok = MagicMock(returncode=0, stderr=b"", stdout=b"Configuration OK.")
        side = [sp.TimeoutExpired(cmd="xray", timeout=xray.VALIDATE_TIMEOUT_S), ok]
        with (
            patch("app.services.xray.os.path.exists", return_value=True),
            patch("app.services.xray.subprocess.run", side_effect=side) as run,
        ):
            xray._validate_xray_config("/tmp/cfg.json")
            assert run.call_count == 2

    def test_fails_closed_on_persistent_timeout(self):
        import subprocess as sp

        from app.services import xray

        side = sp.TimeoutExpired(cmd="xray", timeout=xray.VALIDATE_TIMEOUT_S)
        with (
            patch("app.services.xray.os.path.exists", return_value=True),
            patch("app.services.xray.subprocess.run", side_effect=side),
        ):
            with pytest.raises(ValueError, match="timed out"):
                xray._validate_xray_config("/tmp/cfg.json")
