"""Aggregated subscription: all of a user's keys via /api/sub/u/<token>."""

import base64
import json

import pytest

from app.extensions import db
from app.models import Client, Inbound, TelegramUser


REALITY_STREAM = json.dumps(
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


@pytest.fixture
def app(app):
    from app.api import subscription as sub_api

    if "subscription" not in app.blueprints:
        app.register_blueprint(sub_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def user_with_two_keys(app):
    """A TelegramUser with two enabled clients on two inbounds. Returns sub_token."""
    with app.app_context():
        for i, tag in enumerate(("de-reality", "nl-reality"), start=1):
            db.session.add(
                Inbound(tag=tag, port=443 + i, protocol="vless", stream_settings=REALITY_STREAM, label=tag.upper())
            )
        db.session.flush()
        u = TelegramUser(telegram_id=700, sub_token="tok-700-aaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        db.session.add(u)
        db.session.add(
            Client(
                id="aaaaaaaa-0000-0000-0000-000000000001",
                email="u700_de",
                inbound_tag="de-reality",
                telegram_id=700,
                enable=True,
            )
        )
        db.session.add(
            Client(
                id="bbbbbbbb-0000-0000-0000-000000000002",
                email="u700_nl",
                inbound_tag="nl-reality",
                telegram_id=700,
                enable=True,
            )
        )
        db.session.commit()
        return "tok-700-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_aggregate_collects_all_local_keys(app, user_with_two_keys):
    from app.api.subscription import get_subscription_content_for_user

    with app.app_context():
        links = get_subscription_content_for_user(700)
        assert links is not None
        assert len(links) == 2
        assert all(link.startswith("vless://") for link in links)
        joined = "\n".join(links)
        assert "DE-REALITY" in joined and "NL-REALITY" in joined


def test_aggregate_clash_merges_proxies(app, user_with_two_keys):
    import yaml
    from app.api.subscription import generate_clash_config_for_user

    with app.app_context():
        doc = generate_clash_config_for_user(700)
        assert doc is not None
        parsed = yaml.safe_load(doc)
        assert len(parsed["proxies"]) == 2
        names = [p["name"] for p in parsed["proxies"]]
        assert parsed["proxy-groups"][0]["proxies"] == names
        assert parsed["proxy-groups"][0]["type"] == "url-test"
        # skeleton preserved: rules carried over from the per-client base doc
        assert "rules" in parsed


def test_aggregate_singbox_merges_outbounds(app, user_with_two_keys):
    import json as _json
    from app.api.subscription import generate_singbox_config_for_user

    with app.app_context():
        doc = generate_singbox_config_for_user(700)
        assert doc is not None
        parsed = _json.loads(doc)
        tags = [
            o.get("tag")
            for o in parsed["outbounds"]
            if o.get("type") not in ("selector", "urltest", "direct", "block", "dns")
        ]
        assert len([t for t in tags if t]) >= 2
        # skeleton preserved: tun inbounds + dns carried over from the base doc
        assert parsed.get("inbounds")
        assert "dns" in parsed
        # no dangling refs to the old hardcoded per-client "proxy" tag
        assert all(s.get("detour") != "proxy" for s in parsed.get("dns", {}).get("servers", []) if isinstance(s, dict))
        assert parsed.get("route", {}).get("final") != "proxy"
        # the merged PROXY selector exists
        assert any(o.get("tag") == "PROXY" and o.get("type") == "selector" for o in parsed["outbounds"])


def test_aggregate_headers_pick_nearest_to_exhaustion(app):
    from app.api.subscription import _aggregate_user_headers
    from app.models import Client

    # key A: 90/100 GB used (10 left) ; key B: 10/100 GB used (90 left) -> A is nearest
    gb = 1024**3
    a = Client(
        id="a",
        email="a",
        inbound_tag="x",
        enable=True,
        up=90 * gb,
        down=0,
        limit_bytes=100 * gb,
        expiry_time=2000_000,
    )
    b = Client(
        id="b",
        email="b",
        inbound_tag="y",
        enable=True,
        up=10 * gb,
        down=0,
        limit_bytes=100 * gb,
        expiry_time=1000_000,
    )
    headers = _aggregate_user_headers([a, b])
    info = headers["subscription-userinfo"]
    assert f"total={100 * gb}" in info
    assert f"upload={90 * gb}" in info  # from key A (nearest to exhaustion)
    assert "expire=1000" in info  # nearest expiry (1000_000 ms -> 1000 s)


def test_aggregate_headers_all_unlimited(app):
    from app.api.subscription import _aggregate_user_headers
    from app.models import Client

    a = Client(id="a", email="a", inbound_tag="x", enable=True, up=5, down=5, limit_bytes=0, expiry_time=0)
    headers = _aggregate_user_headers([a])
    assert "total=0" in headers["subscription-userinfo"]


def test_endpoint_returns_aggregated_base64(client, user_with_two_keys):
    token = user_with_two_keys
    resp = client.get(f"/api/sub/u/{token}", headers={"User-Agent": "v2rayng"})
    assert resp.status_code == 200
    decoded = base64.b64decode(resp.data).decode()
    assert decoded.count("vless://") == 2
    assert "subscription-userinfo" in resp.headers


def test_endpoint_unknown_token_404(client):
    resp = client.get("/api/sub/u/nonexistent-token", headers={"User-Agent": "v2rayng"})
    assert resp.status_code == 404


def test_endpoint_does_not_collide_with_per_client_route(client, user_with_two_keys):
    # /sub/u/<token> must hit the aggregate handler, not /sub/<path:uuid_str>
    token = user_with_two_keys
    resp = client.get(f"/api/sub/u/{token}", headers={"User-Agent": "v2rayng"})
    assert resp.status_code == 200  # aggregate, not "User not found"


def test_aggregate_header_counts_remote_child_panel_key(client, app, user_with_two_keys, monkeypatch):
    """The aggregated subscription-userinfo header must count a user's child-panel keys.

    Local keys for telegram_id 700 are unlimited (no limit_bytes). A remote child-panel
    key is 95/100 GB used (nearest to exhaustion) with an earlier expiry, so the header
    must reflect the remote key's upload/total/expire — not just the local unlimited keys.
    """
    from app.models import LinkedPanel
    from app.services import panel_proxy

    gb = 1024**3
    with app.app_context():
        db.session.add(
            LinkedPanel(
                name="child-1",
                url="https://child.example.com",
                federation_token="fed-tok-child-1",
                enable=True,
                status="online",
                created_at=0,
            )
        )
        db.session.commit()

    fake_snapshot = {
        "panel_name": "child-1",
        "status": "ok",
        "inbounds": [
            {
                "tag": "se-reality",
                "port": 8443,
                "protocol": "vless",
                "label": "SE-REALITY",
                "stream_settings": json.loads(REALITY_STREAM),
                "clients": [
                    {
                        "id": "cccccccc-0000-0000-0000-000000000003",
                        "email": "u700_se",
                        "enable": True,
                        "up": 95 * gb,
                        "down": 0,
                        "limit_bytes": 100 * gb,
                        "expiry_time": 500_000,
                        "telegram_id": 700,
                    }
                ],
            }
        ],
    }

    monkeypatch.setattr(panel_proxy, "get_panel_snapshot", lambda panel_id: fake_snapshot)

    token = user_with_two_keys
    resp = client.get(f"/api/sub/u/{token}", headers={"User-Agent": "v2rayng"})
    assert resp.status_code == 200
    info = resp.headers["subscription-userinfo"]
    assert f"upload={95 * gb}" in info  # from the remote key (nearest to exhaustion)
    assert f"total={100 * gb}" in info  # remote key's limit, not local total=0
    assert "expire=500" in info  # remote key's nearer expiry (500_000 ms -> 500 s)


def test_invalidate_user_aggregate_clears_token_keys(app, user_with_two_keys, monkeypatch):
    from app.services import sub_cache

    store = {}

    class FakeRedis:
        def get(self, k):
            return store.get(k)

        def setex(self, k, ttl, v):
            store[k] = v

        def delete(self, *ks):
            for k in ks:
                store.pop(k, None)

    monkeypatch.setattr(sub_cache, "get_redis", lambda: FakeRedis())
    token = user_with_two_keys
    with app.app_context():
        sub_cache.set("u-v2ray", token, "cached-body")
        assert sub_cache.get("u-v2ray", token) in (b"cached-body", "cached-body")
        sub_cache.invalidate_user_aggregate(700)
        assert sub_cache.get("u-v2ray", token) is None


def test_aggregate_header_profile_title_default(app):
    from app.api.subscription import _aggregate_user_headers
    from app.models import Client

    c = Client(id="x", email="x", inbound_tag="t", enable=True, up=0, down=0, limit_bytes=0, expiry_time=0)
    headers = _aggregate_user_headers([c])
    # default title when no brand_name SystemSetting is set
    assert headers.get("profile-title") == "Subscription"


def test_aggregate_header_profile_title_ascii_brand(app):
    from app.api.subscription import _aggregate_user_headers
    from app.models import Client, SystemSetting
    from app.extensions import db

    db.session.add(SystemSetting(key="brand_name", value="ACME VPN"))
    db.session.commit()
    c = Client(id="x", email="x", inbound_tag="t", enable=True, up=0, down=0, limit_bytes=0, expiry_time=0)
    headers = _aggregate_user_headers([c])
    assert headers.get("profile-title") == "ACME VPN"  # ASCII -> plain


def test_aggregate_header_profile_title_unicode_brand_is_base64(app):
    import base64

    from app.api.subscription import _aggregate_user_headers
    from app.models import Client, SystemSetting
    from app.extensions import db

    db.session.add(SystemSetting(key="brand_name", value="МойВПН"))
    db.session.commit()
    c = Client(id="x", email="x", inbound_tag="t", enable=True, up=0, down=0, limit_bytes=0, expiry_time=0)
    headers = _aggregate_user_headers([c])
    title = headers.get("profile-title")
    assert title.startswith("base64:")  # non-ASCII -> base64-encoded, HTTP-safe
    assert base64.b64decode(title[len("base64:") :]).decode("utf-8") == "МойВПН"
    # header value must be latin-1 encodable (valid HTTP header)
    title.encode("latin-1")


def test_aggregate_header_profile_title_accented_latin_is_base64(app):
    # latin-1-but-non-ASCII (e.g. "é") must also be base64'd so UTF-8-decoding
    # clients render it correctly instead of as mojibake.
    import base64

    from app.api.subscription import _aggregate_user_headers
    from app.models import Client, SystemSetting
    from app.extensions import db

    db.session.add(SystemSetting(key="brand_name", value="Café VPN"))
    db.session.commit()
    c = Client(id="x", email="x", inbound_tag="t", enable=True, up=0, down=0, limit_bytes=0, expiry_time=0)
    headers = _aggregate_user_headers([c])
    title = headers.get("profile-title")
    assert title.startswith("base64:")
    assert base64.b64decode(title[len("base64:") :]).decode("utf-8") == "Café VPN"
    title.encode("latin-1")  # still a valid HTTP header value


def test_aggregate_blocks_new_device_over_limit(client, app, user_with_two_keys):
    import base64

    from app.extensions import db
    from app.models import SystemSetting

    token = user_with_two_keys
    with app.app_context():
        db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
        db.session.add(SystemSetting(key="device_limit_per_user", value="1"))
        db.session.commit()
    r1 = client.get(f"/api/sub/u/{token}", headers={"User-Agent": "v2rayng", "x-hwid": "devA"})
    assert r1.status_code == 200
    assert base64.b64decode(r1.data).decode().count("vless://") >= 1
    r2 = client.get(f"/api/sub/u/{token}", headers={"User-Agent": "v2rayng", "x-hwid": "devB"})
    assert r2.status_code == 200
    decoded = base64.b64decode(r2.data).decode()
    assert "127.0.0.1" in decoded  # warn placeholder, not the real nodes


def test_aggregate_no_gate_when_toggle_off(client, user_with_two_keys):
    import base64

    token = user_with_two_keys
    r = client.get(f"/api/sub/u/{token}", headers={"User-Agent": "v2rayng", "x-hwid": "devX"})
    assert base64.b64decode(r.data).decode().count("vless://") >= 1


def test_browser_never_gated_even_over_limit(client, app, user_with_two_keys):
    from app.extensions import db
    from app.models import SystemSetting

    token = user_with_two_keys
    with app.app_context():
        db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
        db.session.add(SystemSetting(key="device_limit_per_user", value="1"))
        db.session.commit()
    # fill the limit via a client UA
    client.get(f"/api/sub/u/{token}", headers={"User-Agent": "v2rayng", "x-hwid": "devA"})
    # a browser sending a new over-limit hwid must STILL get the HTML page (never a warn config)
    resp = client.get(
        f"/api/sub/u/{token}",
        headers={"User-Agent": "Mozilla/5.0 AppleWebKit/537.36", "x-hwid": "devB"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert "<!doctype html>" in resp.get_data(as_text=True).lower()
