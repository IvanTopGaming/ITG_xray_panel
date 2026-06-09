"""Tests for the subscription API (GET /api/sub/<uuid>).

Subscription endpoints are public (no auth) — accessed by UUID.
"""

import base64
import json
import time
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Client, Inbound, SystemSetting


# ── Fixtures ────────────────────────────────────────────────────────────


VLESS_STREAM = json.dumps(
    {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
            "serverNames": ["google.com"],
            "publicKey": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "shortIds": ["abcd1234"],
            "fingerprint": "chrome",
            "spiderX": "",
        },
    }
)

TEST_UUID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def app(app):
    """Extend the base app fixture with the subscription blueprint."""
    from app.api import subscription as sub_api

    if not any(bp_name == "subscription" for bp_name in app.blueprints):
        app.register_blueprint(sub_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def seed_vless(app):
    """Seed an Inbound + Client for VLESS-reality testing. Return the client UUID."""
    with app.app_context():
        ib = Inbound(
            tag="vless-reality",
            port=443,
            protocol="vless",
            stream_settings=VLESS_STREAM,
            label="DE Reality",
        )
        db.session.add(ib)
        db.session.flush()
        c = Client(
            id=TEST_UUID,
            email="alice",
            inbound_tag="vless-reality",
            enable=True,
            up=1024,
            down=2048,
            limit_bytes=10737418240,  # 10 GB
            expiry_time=int(time.time() * 1000) + 86400_000 * 30,  # 30 days
            flow="xtls-rprx-vision",
        )
        db.session.add(c)
        db.session.commit()
    return TEST_UUID


@pytest.fixture
def seed_disabled(app):
    """Seed an Inbound + disabled Client. Return the client UUID."""
    disabled_uuid = "22222222-2222-2222-2222-222222222222"
    with app.app_context():
        # Reuse existing inbound if possible.
        if not Inbound.query.filter_by(tag="vless-reality").first():
            ib = Inbound(
                tag="vless-reality",
                port=443,
                protocol="vless",
                stream_settings=VLESS_STREAM,
                label="DE Reality",
            )
            db.session.add(ib)
            db.session.flush()
        c = Client(
            id=disabled_uuid,
            email="bob-disabled",
            inbound_tag="vless-reality",
            enable=False,
            up=0,
            down=0,
            limit_bytes=0,
            expiry_time=0,
        )
        db.session.add(c)
        db.session.commit()
    return disabled_uuid


# ── Helpers ──────────────────────────────────────────────────────────────

# Common mock targets used to bypass federation proxy + device tracking.
# These are imported lazily inside functions, so we patch at the source module.
_PATCH_PANEL = "app.services.panel_proxy.get_panel_snapshot"
_PATCH_DEVICE_GATE = "app.services.device_tracking.device_gate"
_PATCH_SUB_CACHE_GET = "app.services.sub_cache.get"
_PATCH_SUB_CACHE_SET = "app.services.sub_cache.set"


def _device_gate_ok(client_obj, inbound_obj, headers):
    """device_gate stub that always returns ok with no extra headers."""
    return ("ok", {})


def _proxy_app_ua(ua="v2rayNG/1.0"):
    """Return a User-Agent header dict that looks like a proxy app, not a browser."""
    return {"User-Agent": ua}


def _browser_ua():
    """Return a User-Agent header dict that identifies as a browser."""
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }


# ── Tests: basic v2ray (base64) subscription ─────────────────────────────


class TestV2RaySubscription:
    """GET /api/sub/<uuid> with proxy-app User-Agent → base64-encoded link(s)."""

    def test_returns_base64_vless_link(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        assert resp.status_code == 200
        assert resp.content_type.startswith("text/plain")

        decoded = base64.b64decode(resp.data).decode("utf-8")
        assert decoded.startswith("vless://")
        assert TEST_UUID in decoded
        assert "reality" in decoded
        assert "google.com" in decoded

    def test_404_for_unknown_uuid(self, client):
        unknown = "99999999-9999-9999-9999-999999999999"
        with patch(_PATCH_PANEL, return_value=None):
            resp = client.get(f"/api/sub/{unknown}", headers=_proxy_app_ua())

        assert resp.status_code == 404

    def test_404_for_disabled_client(self, client, seed_disabled):
        with patch(_PATCH_PANEL, return_value=None):
            resp = client.get(f"/api/sub/{seed_disabled}", headers=_proxy_app_ua())

        assert resp.status_code == 404


# ── Tests: subscription-userinfo header ──────────────────────────────────


class TestSubscriptionUserinfo:
    """Verify the subscription-userinfo header carries traffic/limit/expiry."""

    def test_header_present_with_correct_fields(self, client, seed_vless, app):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        assert resp.status_code == 200
        userinfo = resp.headers.get("subscription-userinfo")
        assert userinfo is not None

        # Parse the header — format: upload=N; download=N; total=N; expire=N
        parts = {p.split("=")[0].strip(): int(p.split("=")[1]) for p in userinfo.split(";")}
        assert parts["upload"] == 1024
        assert parts["download"] == 2048
        assert parts["total"] == 10737418240
        assert parts["expire"] > 0  # future timestamp in seconds

    def test_profile_update_interval_header(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        assert resp.status_code == 200
        assert "Profile-Update-Interval" in resp.headers

    def test_custom_update_interval(self, client, seed_vless, app):
        """When subscription_update_interval_hours is set, it reflects in the header."""
        with app.app_context():
            db.session.add(SystemSetting(key="subscription_update_interval_hours", value="6"))
            db.session.commit()

        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        assert resp.status_code == 200
        assert resp.headers.get("Profile-Update-Interval") == "6"


# ── Tests: Content-Disposition ──────────────────────────────────────────


class TestContentDisposition:
    """Content-Disposition header with a personalised filename."""

    def test_contains_filename(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        assert resp.status_code == 200
        cd = resp.headers.get("Content-Disposition", "")
        assert "attachment" in cd
        # Filename is derived from client.email ("alice"), extension stripped.
        assert "alice" in cd


# ── Tests: browser landing page ──────────────────────────────────────────


class TestBrowserGetsConfig:
    """Variant A: the per-client endpoint serves config to everyone (no HTML page)."""

    def test_browser_ua_gets_config_not_page(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_browser_ua())
        assert resp.status_code == 200
        assert "text/html" not in resp.content_type
        import base64 as _b64

        assert "vless://" in _b64.b64decode(resp.data).decode("utf-8")

    def test_browser_unknown_uuid_404(self, client):
        unknown = "99999999-9999-9999-9999-999999999999"
        with patch(_PATCH_PANEL, return_value=None):
            resp = client.get(f"/api/sub/{unknown}", headers=_browser_ua())
        assert resp.status_code == 404


# ── Tests: Clash format ──────────────────────────────────────────────────


class TestClashSubscription:
    """Clash-style UA → YAML config."""

    def test_returns_yaml_for_clash_ua(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(
                f"/api/sub/{seed_vless}",
                headers={"User-Agent": "clash-meta/1.0"},
            )

        assert resp.status_code == 200
        assert "yaml" in resp.content_type
        body = resp.data.decode("utf-8")
        assert "proxies:" in body
        assert "vless" in body


# ── Tests: sing-box format ──────────────────────────────────────────────


class TestSingboxSubscription:
    """sing-box UA → JSON config."""

    def test_returns_json_for_singbox_ua(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(
                f"/api/sub/{seed_vless}",
                headers={"User-Agent": "sing-box/0.18"},
            )

        assert resp.status_code == 200
        assert "json" in resp.content_type
        data = json.loads(resp.data)
        assert "outbounds" in data
        assert any(o.get("type") == "vless" for o in data["outbounds"])


# ── Tests: forced UA via ?ua= query param ────────────────────────────────


class TestForcedUAParam:
    """?ua= query parameter overrides detected User-Agent for format selection."""

    def test_ua_clash_returns_yaml(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            # Use a browser UA but force clash format.
            resp = client.get(
                f"/api/sub/{seed_vless}?ua=clash",
                headers=_browser_ua(),
            )

        assert resp.status_code == 200
        assert "yaml" in resp.content_type

    def test_ua_singbox_returns_json(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(
                f"/api/sub/{seed_vless}?ua=singbox",
                headers=_browser_ua(),
            )

        assert resp.status_code == 200
        assert "json" in resp.content_type

    def test_ua_v2ray_returns_base64(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(
                f"/api/sub/{seed_vless}?ua=v2ray",
                headers=_browser_ua(),
            )

        assert resp.status_code == 200
        assert resp.content_type.startswith("text/plain")
        decoded = base64.b64decode(resp.data).decode("utf-8")
        assert "vless://" in decoded


# ── Tests: device gate warn-config ───────────────────────────────────────


class TestDeviceGateWarnConfig:
    """When device_gate returns a non-ok state, a warn-config is served."""

    def test_unsupported_state_returns_warn(self, client, seed_vless):
        def _gate_unsupported(c, ib, hdrs):
            return ("unsupported", {"x-hwid-active": "true"})

        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_gate_unsupported),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        assert resp.status_code == 200
        decoded = base64.b64decode(resp.data).decode("utf-8")
        assert "unsupported" in decoded.lower() or "127.0.0.1" in decoded

    def test_limit_state_returns_warn(self, client, seed_vless):
        def _gate_limit(c, ib, hdrs):
            return ("limit", {"x-hwid-active": "true"})

        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_gate_limit),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        assert resp.status_code == 200
        decoded = base64.b64decode(resp.data).decode("utf-8")
        assert "limit" in decoded.lower() or "127.0.0.1" in decoded


# ── Tests: VLESS link content ────────────────────────────────────────────


class TestVlessLinkContent:
    """Verify the actual VLESS link contains all expected Reality parameters."""

    def test_link_contains_reality_params(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        decoded = base64.b64decode(resp.data).decode("utf-8")
        # Check Reality fields.
        assert "pbk=" in decoded
        assert "fp=chrome" in decoded
        assert "sni=google.com" in decoded
        assert "sid=abcd1234" in decoded
        assert "security=reality" in decoded

    def test_link_contains_flow(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        decoded = base64.b64decode(resp.data).decode("utf-8")
        assert "flow=xtls-rprx-vision" in decoded

    def test_link_label_is_url_fragment(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        decoded = base64.b64decode(resp.data).decode("utf-8")
        # The label "DE Reality" appears as URL-encoded fragment.
        assert "DE%20Reality" in decoded


# ── Tests: profile-title header ──────────────────────────────────────────


class TestProfileTitle:
    """profile-title header carries the client email."""

    def test_profile_title_matches_email(self, client, seed_vless):
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=None),
            patch(_PATCH_SUB_CACHE_SET),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        assert resp.headers.get("profile-title") == "alice"


# ── Tests: sub_cache integration ─────────────────────────────────────────


class TestSubCacheHit:
    """When sub_cache returns a cached value, the endpoint serves it directly."""

    def test_cache_hit_skips_link_generation(self, client, seed_vless):
        cached_blob = base64.b64encode(b"vless://cached@host:443").decode()
        with (
            patch(_PATCH_PANEL, return_value=None),
            patch(_PATCH_DEVICE_GATE, side_effect=_device_gate_ok),
            patch(_PATCH_SUB_CACHE_GET, return_value=cached_blob),
        ):
            resp = client.get(f"/api/sub/{seed_vless}", headers=_proxy_app_ua())

        assert resp.status_code == 200
        assert resp.data.decode() == cached_blob


# ── Regression: XHTTP transport host/path in generated subscription links ────
# Stored stream_settings is NESTED (xhttpSettings/wsSettings/... built by
# _build_stream_settings). The federation REMOTE link builder never emitted the
# transport host/path, so xhttp/ws subscriptions from linked panels were broken
# (the real bug). _extract_transport_path_host also accepts the flat wsPath/
# wsHost keys defensively (legacy/import blobs).


def test_extract_transport_path_host_reads_nested_and_flat():
    from app.api.subscription import _extract_transport_path_host

    # real storage shape: nested xhttpSettings (what _build_stream_settings emits)
    assert _extract_transport_path_host(
        {"network": "xhttp", "xhttpSettings": {"path": "/realpath", "host": "edge.example.com"}}
    ) == ("/realpath", "edge.example.com")
    # defensive: flat wsPath/wsHost (legacy/import blobs) still work
    assert _extract_transport_path_host({"network": "xhttp", "wsPath": "/flat", "wsHost": "cdn.example.com"}) == (
        "/flat",
        "cdn.example.com",
    )
    # leading slash is normalized
    assert _extract_transport_path_host({"network": "ws", "wsPath": "noslash"}) == ("/noslash", "")
    # tcp / grpc carry no transport path/host
    assert _extract_transport_path_host({"network": "tcp"}) == ("", "")
    assert _extract_transport_path_host({"network": "grpc"}) == ("", "")


def test_local_xhttp_link_includes_path_and_host(app):
    """Guard: a locally-stored (nested) xhttp inbound surfaces host+path."""
    from urllib.parse import urlparse, parse_qs
    from app.api.subscription import _get_local_subscription_content

    with app.app_context():
        ib = Inbound(
            tag="xhttp-tls",
            port=443,
            protocol="vless",
            stream_settings=json.dumps(
                {
                    "network": "xhttp",
                    "security": "tls",
                    "xhttpSettings": {"path": "/throne", "host": "edge.example.com"},
                    "tlsSettings": {"serverName": "edge.example.com"},
                }
            ),
            label="XHTTP",
        )
        db.session.add(ib)
        db.session.flush()
        uid = "33333333-3333-3333-3333-333333333333"
        db.session.add(Client(id=uid, email="x", inbound_tag="xhttp-tls", enable=True, expiry_time=0))
        db.session.commit()
        links = _get_local_subscription_content(uid)

    assert links and len(links) == 1
    q = parse_qs(urlparse(links[0]).query)
    assert q.get("type") == ["xhttp"]
    assert q.get("path") == ["/throne"]
    assert q.get("host") == ["edge.example.com"]


def test_remote_xhttp_link_includes_path_and_host():
    """The real bug: federation remote builder must emit xhttp host+path
    (child snapshots store the nested xhttpSettings blob)."""
    from urllib.parse import urlparse, parse_qs
    from app.api.subscription import _build_remote_link

    ib_data = {
        "protocol": "vless",
        "port": 443,
        "tag": "child-xhttp",
        "label": "Child XHTTP",
        "stream_settings": {
            "network": "xhttp",
            "security": "tls",
            "xhttpSettings": {"path": "/childpath", "host": "child.example.com"},
            "tlsSettings": {"serverName": "child.example.com"},
        },
    }
    links = _build_remote_link("child.example.com", ib_data, {"id": "00000000-0000-0000-0000-0000000000aa", "flow": ""})
    assert len(links) == 1
    q = parse_qs(urlparse(links[0]).query)
    assert q.get("type") == ["xhttp"]
    assert q.get("path") == ["/childpath"]
    assert q.get("host") == ["child.example.com"]
    # remote grpc serviceName must come from the child's real grpcSettings too
    grpc_ib = {
        "protocol": "vless",
        "port": 443,
        "tag": "child-grpc",
        "label": "gRPC",
        "stream_settings": {"network": "grpc", "security": "tls", "grpcSettings": {"serviceName": "mygrpc"}},
    }
    g = parse_qs(urlparse(_build_remote_link("c", grpc_ib, {"id": "x", "flow": ""})[0]).query)
    assert g.get("serviceName") == ["mygrpc"]


def test_local_and_remote_builders_are_unified():
    """Unification guard: both paths go through _build_share_links, so the same
    inbound+client must yield byte-identical links (they can no longer drift)."""
    from app.api.subscription import _build_share_links, _build_remote_link

    stream = {
        "network": "xhttp",
        "security": "tls",
        "xhttpSettings": {"path": "/p", "host": "h.example.com"},
        "tlsSettings": {"serverName": "h.example.com"},
    }
    for proto in ("vless", "vmess", "trojan"):
        ib_data = {"protocol": proto, "port": 443, "tag": "t", "label": "L", "stream_settings": stream}
        remote = _build_remote_link("host.example.com", ib_data, {"id": "uuid-9", "flow": ""})
        direct = _build_share_links("host.example.com", proto, 443, stream, "uuid-9", "", "L")
        assert remote == direct, f"local/remote diverge for {proto}"


# ── Full matrix: protocol × transport × security → link generation ───────────
# Covers every share-link shape through the real storage path
# (_build_stream_settings → nested blob) and asserts local == remote and that
# each link carries the right transport + security params.

import pytest as _pytest
from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs

_REALITY_PRIV = "UDpHHj1ZCyLcFk4ZG6zKS2B8YLPNPtdQzADuJf_vTXY"  # valid x25519 key; pubkey is derived

_MATRIX = [
    ("vless", "tcp", "reality"),
    ("vless", "tcp", "tls"),
    ("vless", "ws", "tls"),
    ("vless", "ws", "none"),
    ("vless", "xhttp", "tls"),
    ("vless", "xhttp", "reality"),
    ("vless", "grpc", "tls"),
    ("vless", "grpc", "reality"),
    ("vless", "httpupgrade", "tls"),
    ("vless", "splithttp", "tls"),
    ("vmess", "tcp", "none"),
    ("vmess", "ws", "tls"),
    ("vmess", "xhttp", "tls"),
    ("vmess", "grpc", "tls"),
    ("trojan", "tcp", "tls"),
    ("trojan", "ws", "tls"),
    ("trojan", "xhttp", "tls"),
    ("trojan", "grpc", "tls"),
    ("trojan", "tcp", "reality"),
]


def _matrix_payload(proto, net, sec):
    p = {
        "protocol": proto,
        "network": net,
        "security": sec,
        "wsPath": "/mypath",
        "wsHost": "h.example.com",
        "grpcServiceName": "mysvc",
        "tlsServerName": "tls.example.com",
        "tlsAlpn": "h2,http/1.1",
        "tlsUTLSFingerprint": "chrome",
    }
    if sec == "reality":
        p.update(
            {
                "realitySNI": "rl.example.com",
                "realityShortIds": "abcd1234",
                "realityFingerprint": "chrome",
                "realityPrivateKey": _REALITY_PRIV,
                "realityDest": "google.com:443",
            }
        )
    return p


@_pytest.mark.parametrize("proto,net,sec", _MATRIX, ids=lambda v: v if isinstance(v, str) else "")
def test_link_matrix(proto, net, sec):
    from app.services.xray import _build_stream_settings
    from app.api.subscription import (
        _build_share_links,
        _build_remote_link,
        _apply_clash_transport,
        _apply_singbox_transport,
    )

    stream = _build_stream_settings(_matrix_payload(proto, net, sec))
    flow = "xtls-rprx-vision" if (proto == "vless" and net == "tcp" and sec in ("reality", "tls")) else ""
    uuid = "11111111-1111-1111-1111-111111111111"

    local = _build_share_links("host.example.com", proto, 443, stream, uuid, flow, "Lbl")
    remote = _build_remote_link(
        "host.example.com",
        {"protocol": proto, "port": 443, "label": "Lbl", "stream_settings": stream},
        {"id": uuid, "flow": flow},
    )
    assert local and remote, f"{proto}/{net}/{sec}: empty link"
    assert local == remote, f"{proto}/{net}/{sec}: local != remote"

    link = local[0]
    if proto in ("vless", "trojan"):
        q = _parse_qs(_urlparse(link).query)
        assert q.get("type") == [net]
        assert q.get("security") == [sec]
        if net in ("ws", "xhttp", "httpupgrade", "splithttp"):
            assert q.get("path") == ["/mypath"], f"{proto}/{net}: missing path"
            assert q.get("host") == ["h.example.com"], f"{proto}/{net}: missing host"
        elif net == "grpc":
            assert q.get("serviceName") == ["mysvc"], f"{proto}/{net}: missing serviceName"
        if sec == "reality":
            assert q.get("pbk") and q.get("sni") == ["rl.example.com"] and q.get("sid") == ["abcd1234"]
        elif sec == "tls":
            assert q.get("sni") == ["tls.example.com"]
        if flow:
            assert q.get("flow") == [flow]
    elif proto == "vmess":
        conf = json.loads(base64.b64decode(link[len("vmess://") :]))
        assert conf["net"] == net
        if net in ("ws", "xhttp", "httpupgrade", "splithttp"):
            assert conf["path"] == "/mypath" and conf["host"] == "h.example.com"
        elif net == "grpc":
            assert conf["path"] == "mysvc"
        if sec == "tls":
            assert conf.get("sni") == "tls.example.com"

    # Clash + sing-box transport appliers must produce well-formed structure
    cnode = {}
    _apply_clash_transport(cnode, stream)
    sob = {}
    _apply_singbox_transport(sob, stream)
    if net in ("ws", "httpupgrade"):
        assert cnode.get("network") == "ws" and cnode.get("ws-opts", {}).get("path") == "/mypath"
        assert sob["transport"]["path"] == "/mypath"
    elif net in ("xhttp", "splithttp"):
        assert cnode.get("network") == "http" and cnode.get("http-opts", {}).get("path") == ["/mypath"]
        assert sob["transport"]["type"] == "http"
    elif net == "grpc":
        assert cnode.get("grpc-opts", {}).get("grpc-service-name") == "mysvc"
        assert sob["transport"]["service_name"] == "mysvc"


def test_clash_and_singbox_for_user_merge_federation_nodes(app):
    """The aggregated Clash + sing-box configs must include child-panel (remote)
    nodes, not just local keys (regression for the federation-merge gap)."""
    import yaml as _yaml
    from unittest.mock import patch
    from app.models import LinkedPanel
    from app.api.subscription import generate_clash_config_for_user, generate_singbox_config_for_user

    TG = 880017
    remote_snapshot = {
        "inbounds": [
            {
                "tag": "child-xhttp",
                "port": 443,
                "protocol": "vless",
                "label": "Child XHTTP",
                "stream_settings": json.dumps(
                    {
                        "network": "xhttp",
                        "security": "tls",
                        "xhttpSettings": {"path": "/childp", "host": "child.example.com"},
                        "tlsSettings": {"serverName": "child.example.com"},
                    }
                ),
                "clients": [
                    {
                        "id": "aaaaaaaa-0000-0000-0000-000000000001",
                        "email": "ru",
                        "enable": True,
                        "telegram_id": TG,
                        "flow": "",
                    }
                ],
            }
        ]
    }
    with app.app_context():
        db.session.add(
            LinkedPanel(
                name="child", url="https://child.example.com/x", federation_token="t", enable=True, created_at=0
            )
        )
        ib = Inbound(
            tag="loc-vless-ws",
            port=443,
            protocol="vless",
            stream_settings=json.dumps(
                {
                    "network": "ws",
                    "security": "tls",
                    "wsSettings": {"path": "/loc"},
                    "tlsSettings": {"serverName": "loc.example.com"},
                }
            ),
            label="Local WS",
        )
        db.session.add(ib)
        db.session.flush()
        db.session.add(
            Client(
                id="bbbbbbbb-0000-0000-0000-000000000002",
                email="loc",
                inbound_tag="loc-vless-ws",
                enable=True,
                telegram_id=TG,
                expiry_time=0,
            )
        )
        db.session.commit()

        with patch("app.services.panel_proxy.get_panel_snapshot", return_value=remote_snapshot):
            clash = generate_clash_config_for_user(TG)
            singbox = generate_singbox_config_for_user(TG)

    cproxies = _yaml.safe_load(clash)["proxies"]
    cnames = [p["name"] for p in cproxies]
    assert len(cproxies) == 2, cnames
    assert any("Child XHTTP" in n for n in cnames), cnames  # remote node present
    child = next(p for p in cproxies if "Child XHTTP" in p["name"])
    assert child["server"] == "child.example.com" and child["network"] == "http"
    assert child["http-opts"]["path"] == ["/childp"]

    sout = [o for o in json.loads(singbox)["outbounds"] if o.get("type") == "vless"]
    assert len(sout) == 2, [o["tag"] for o in sout]
    schild = next(o for o in sout if "Child XHTTP" in o["tag"])
    assert schild["server"] == "child.example.com" and schild["transport"]["type"] == "http"
