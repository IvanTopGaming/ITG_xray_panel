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
