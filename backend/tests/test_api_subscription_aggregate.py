import base64
import json

import pytest

from panel_core.extensions import db
from panel_core.models import Client, Inbound, TelegramUser


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
    from panel_core.api import subscription as sub_api

    if "subscription" not in app.blueprints:
        app.register_blueprint(sub_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def user_with_two_keys(app):

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
    from panel_core.api.subscription import get_subscription_content_for_user

    with app.app_context():
        links = get_subscription_content_for_user(700)
        assert links is not None
        assert len(links) == 2
        assert all(link.startswith("vless://") for link in links)
        joined = "\n".join(links)
        assert "DE-REALITY" in joined and "NL-REALITY" in joined


def test_aggregate_clash_merges_proxies(app, user_with_two_keys):
    import yaml
    from panel_core.api.subscription import generate_clash_config_for_user

    with app.app_context():
        doc = generate_clash_config_for_user(700)
        assert doc is not None
        parsed = yaml.safe_load(doc)
        assert len(parsed["proxies"]) == 2
        names = [p["name"] for p in parsed["proxies"]]
        assert parsed["proxy-groups"][0]["proxies"] == names
        assert parsed["proxy-groups"][0]["type"] == "url-test"

        assert "rules" in parsed


def test_aggregate_singbox_merges_outbounds(app, user_with_two_keys):
    import json as _json
    from panel_core.api.subscription import generate_singbox_config_for_user

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

        assert parsed.get("inbounds")
        assert "dns" in parsed

        assert all(s.get("detour") != "proxy" for s in parsed.get("dns", {}).get("servers", []) if isinstance(s, dict))
        assert parsed.get("route", {}).get("final") != "proxy"

        assert any(o.get("tag") == "PROXY" and o.get("type") == "selector" for o in parsed["outbounds"])


def test_aggregate_headers_pick_nearest_to_exhaustion(app):
    from panel_core.api.subscription import _aggregate_user_headers
    from panel_core.models import Client

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
    assert f"upload={90 * gb}" in info
    assert "expire=1000" in info


def test_aggregate_headers_all_unlimited(app):
    from panel_core.api.subscription import _aggregate_user_headers
    from panel_core.models import Client

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

    token = user_with_two_keys
    resp = client.get(f"/api/sub/u/{token}", headers={"User-Agent": "v2rayng"})
    assert resp.status_code == 200


def test_aggregate_header_counts_remote_child_panel_key(client, app, user_with_two_keys, monkeypatch):

    from panel_core.models import LinkedPanel
    from panel_core.services import panel_proxy

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
    assert f"upload={95 * gb}" in info
    assert f"total={100 * gb}" in info
    assert "expire=0" in info, (
        "wave 5b: this account's two local keys carry expiry_time=0, which means 'never expires', and "
        "0 absorbs every dated key in the fold (§41, customer decision). The header used to filter the "
        "zeroes out and report the remote key's 500 — the same account then read 'permanent' in Telegram "
        "and a date in the client app. See tests/test_one_answer_for_when_access_ends.py."
    )


def test_invalidate_user_aggregate_clears_token_keys(app, user_with_two_keys, monkeypatch):
    from panel_core.services import sub_cache

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
    from panel_core.api.subscription import _aggregate_user_headers
    from panel_core.models import Client

    c = Client(id="x", email="x", inbound_tag="t", enable=True, up=0, down=0, limit_bytes=0, expiry_time=0)
    headers = _aggregate_user_headers([c])

    assert headers.get("profile-title") == "Subscription"


def test_aggregate_header_profile_title_ascii_brand(app):
    from panel_core.api.subscription import _aggregate_user_headers
    from panel_core.models import Client, SystemSetting
    from panel_core.extensions import db

    db.session.add(SystemSetting(key="brand_name", value="ACME VPN"))
    db.session.commit()
    c = Client(id="x", email="x", inbound_tag="t", enable=True, up=0, down=0, limit_bytes=0, expiry_time=0)
    headers = _aggregate_user_headers([c])
    assert headers.get("profile-title") == "ACME VPN"


def test_aggregate_header_profile_title_unicode_brand_is_base64(app):
    import base64

    from panel_core.api.subscription import _aggregate_user_headers
    from panel_core.models import Client, SystemSetting
    from panel_core.extensions import db

    db.session.add(SystemSetting(key="brand_name", value="МойВПН"))
    db.session.commit()
    c = Client(id="x", email="x", inbound_tag="t", enable=True, up=0, down=0, limit_bytes=0, expiry_time=0)
    headers = _aggregate_user_headers([c])
    title = headers.get("profile-title")
    assert title.startswith("base64:")
    assert base64.b64decode(title[len("base64:") :]).decode("utf-8") == "МойВПН"

    title.encode("latin-1")


def test_aggregate_header_profile_title_accented_latin_is_base64(app):

    import base64

    from panel_core.api.subscription import _aggregate_user_headers
    from panel_core.models import Client, SystemSetting
    from panel_core.extensions import db

    db.session.add(SystemSetting(key="brand_name", value="Café VPN"))
    db.session.commit()
    c = Client(id="x", email="x", inbound_tag="t", enable=True, up=0, down=0, limit_bytes=0, expiry_time=0)
    headers = _aggregate_user_headers([c])
    title = headers.get("profile-title")
    assert title.startswith("base64:")
    assert base64.b64decode(title[len("base64:") :]).decode("utf-8") == "Café VPN"
    title.encode("latin-1")


def test_aggregate_blocks_new_device_over_limit(client, app, user_with_two_keys):
    import base64

    from panel_core.extensions import db
    from panel_core.models import SystemSetting

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
    assert "127.0.0.1" in decoded


def test_aggregate_no_gate_when_toggle_off(client, user_with_two_keys):
    import base64

    token = user_with_two_keys
    r = client.get(f"/api/sub/u/{token}", headers={"User-Agent": "v2rayng", "x-hwid": "devX"})
    assert base64.b64decode(r.data).decode().count("vless://") >= 1


def test_browser_never_gated_even_over_limit(client, app, user_with_two_keys, tmp_path, monkeypatch):
    from panel_core.extensions import db
    from panel_core.models import SystemSetting

    dist = tmp_path / "ui"
    dist.mkdir()
    (dist / "index.html").write_text('<!doctype html><html><body><div id="root"></div></body></html>')
    monkeypatch.setenv("SUB_PAGE_DIST", str(dist))

    token = user_with_two_keys
    with app.app_context():
        db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
        db.session.add(SystemSetting(key="device_limit_per_user", value="1"))
        db.session.commit()

    client.get(f"/api/sub/u/{token}", headers={"User-Agent": "v2rayng", "x-hwid": "devA"})

    resp = client.get(
        f"/api/sub/u/{token}",
        headers={"User-Agent": "Mozilla/5.0 AppleWebKit/537.36", "x-hwid": "devB"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    assert '<div id="root"></div>' in body, (
        "the browser branch must return the page shell before the device gate runs — a browser that has "
        "hit the device limit still needs the page, which is where the user goes to see why"
    )
    assert "<!doctype html>" in body.lower()
