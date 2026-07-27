import json

import pytest

from panel_core.extensions import db
from panel_core.models import Client, ClientDevice, Inbound, SystemSetting, TelegramUser


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

WS_TLS_STREAM = json.dumps(
    {
        "network": "ws",
        "security": "tls",
        "wsSettings": {"path": "/ws"},
        "tlsSettings": {"serverName": "example.com"},
    }
)


def test_protocol_tag_reality():
    from panel_core.api.subscription import _protocol_tag

    assert _protocol_tag("vless", {"network": "tcp", "security": "reality"}) == "Reality"


def test_protocol_tag_vless_ws():
    from panel_core.api.subscription import _protocol_tag

    assert _protocol_tag("vless", {"network": "ws", "security": "tls"}) == "VLESS-WS"


def test_protocol_tag_plain_vless():
    from panel_core.api.subscription import _protocol_tag

    assert _protocol_tag("vless", {"network": "tcp", "security": "tls"}) == "VLESS"


def test_protocol_tag_trojan_vmess_ss():
    from panel_core.api.subscription import _protocol_tag

    assert _protocol_tag("trojan", {"network": "tcp", "security": "tls"}) == "Trojan"
    assert _protocol_tag("vmess", {"network": "ws", "security": "none"}) == "VMess-WS"
    assert _protocol_tag("shadowsocks", {}) == "Shadowsocks"


def test_protocol_tag_accepts_json_string():
    from panel_core.api.subscription import _protocol_tag

    assert _protocol_tag("vless", REALITY_STREAM) == "Reality"


@pytest.fixture
def app(app):
    from panel_core.api import subscription as sub_api

    if "subscription" not in app.blueprints:
        app.register_blueprint(sub_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def user_three_keys(app):

    import time

    now_ms = int(time.time() * 1000)
    gb = 1024**3
    with app.app_context():
        db.session.add(
            Inbound(tag="nl", port=20001, protocol="vless", stream_settings=REALITY_STREAM, label="🇳🇱 Amsterdam")
        )
        db.session.add(
            Inbound(tag="de", port=20002, protocol="vless", stream_settings=WS_TLS_STREAM, label="🇩🇪 Frankfurt")
        )
        db.session.add(
            Inbound(tag="fi", port=20003, protocol="trojan", stream_settings=WS_TLS_STREAM, label="🇫🇮 Helsinki")
        )
        u = TelegramUser(telegram_id=800, sub_token="tok-800-pageaaaaaaaaaaaaaaaaaaaaaaa")
        db.session.add(u)
        db.session.add(
            Client(
                id="c1",
                email="u800_nl",
                inbound_tag="nl",
                telegram_id=800,
                enable=True,
                up=18 * gb,
                down=0,
                limit_bytes=50 * gb,
                expiry_time=now_ms + 12 * 86400_000,
                last_seen=now_ms,
            )
        )
        db.session.add(
            Client(
                id="c2",
                email="u800_de",
                inbound_tag="de",
                telegram_id=800,
                enable=False,
                up=10 * gb,
                down=0,
                limit_bytes=50 * gb,
                expiry_time=now_ms + 5 * 86400_000,
                last_seen=0,
            )
        )
        db.session.add(
            Client(
                id="c3",
                email="u800_fi",
                inbound_tag="fi",
                telegram_id=800,
                enable=True,
                up=7 * gb,
                down=0,
                limit_bytes=0,
                expiry_time=now_ms + 20 * 86400_000,
                last_seen=now_ms,
            )
        )
        db.session.commit()
        return "tok-800-pageaaaaaaaaaaaaaaaaaaaaaaa"


def test_user_page_nodes_collects_local(app, user_three_keys):
    from panel_core.api.subscription import _user_page_nodes

    with app.app_context():
        nodes = _user_page_nodes(800)
        assert len(nodes) == 3
        by_name = {n["name"]: n for n in nodes}
        assert "🇳🇱 Amsterdam" in by_name
        nl = by_name["🇳🇱 Amsterdam"]
        assert nl["tag"] == "Reality"
        assert nl["online"] is True
        assert nl["enabled"] is True
        assert nl["unlimited"] is False
        assert nl["limit"] == 50 * 1024**3

        de = by_name["🇩🇪 Frankfurt"]
        assert de["enabled"] is False
        assert de["online"] is False
        assert de["tag"] == "VLESS-WS"

        fi = by_name["🇫🇮 Helsinki"]
        assert fi["unlimited"] is True
        assert fi["tag"] == "Trojan"


def test_user_page_nodes_empty_user(app):
    from panel_core.api.subscription import _user_page_nodes

    with app.app_context():
        assert _user_page_nodes(999999) == []


def test_device_summary_hidden_without_limit(app, user_three_keys):
    from panel_core.api.subscription import _user_device_summary

    with app.app_context():
        assert _user_device_summary(800) is None


def test_device_summary_counts_unique_hwids(app, user_three_keys):
    import time

    from panel_core.api.subscription import _user_device_summary

    now_ms = int(time.time() * 1000)
    with app.app_context():
        db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
        db.session.add(SystemSetting(key="device_limit_per_user", value="3"))
        db.session.add(ClientDevice(client_id="c1", hwid="hw-A", first_seen=now_ms, last_seen=now_ms))
        db.session.add(ClientDevice(client_id="c3", hwid="hw-A", first_seen=now_ms, last_seen=now_ms))
        db.session.add(ClientDevice(client_id="c3", hwid="hw-B", first_seen=now_ms, last_seen=now_ms))
        db.session.commit()
        summary = _user_device_summary(800)
        assert summary is not None
        assert summary["count"] == 2
        assert summary["limit"] == 3


@pytest.fixture
def http_client(app):
    return app.test_client()


SHELL_MARKER = '<div id="root"></div>'

SHELL_DOC = (
    "The browser branch no longer renders node names, brand or language server-side — it returns the "
    "sub-page bundle's index.html verbatim, and the page fetches /api/sub/u/<token>/info for all of "
    "that (asserted in test_api_subscription_info.py). What stays true at this layer is narrower and "
    "worth pinning on its own: a browser gets HTML rather than a proxy config, and the bytes it gets "
    "do not depend on ?lang= or on any other request-scoped state."
)


@pytest.fixture
def sub_bundle(tmp_path, monkeypatch):
    dist = tmp_path / "ui"
    dist.mkdir()
    (dist / "index.html").write_text(f"<!doctype html><html><body>{SHELL_MARKER}</body></html>")
    monkeypatch.setenv("SUB_PAGE_DIST", str(dist))
    return dist


def test_browser_gets_the_page_shell(http_client, user_three_keys, sub_bundle):
    token = user_three_keys
    resp = http_client.get(
        f"/api/sub/u/{token}",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    assert SHELL_MARKER in body, SHELL_DOC
    assert "<!doctype html>" in body.lower()


def test_the_shell_is_language_independent(http_client, user_three_keys, sub_bundle):
    token = user_three_keys
    headers = {"User-Agent": "Mozilla/5.0 AppleWebKit/537.36"}

    ru = http_client.get(f"/api/sub/u/{token}?lang=ru", headers=headers)
    en = http_client.get(f"/api/sub/u/{token}?lang=en", headers={**headers, "Accept-Language": "en-US,en;q=0.9"})

    assert ru.status_code == 200 and en.status_code == 200
    assert ru.data == en.data, SHELL_DOC


def test_client_ua_still_gets_v2ray(http_client, user_three_keys):
    token = user_three_keys
    resp = http_client.get(f"/api/sub/u/{token}", headers={"User-Agent": "v2rayng/1.8"})
    assert resp.status_code == 200
    assert resp.mimetype.startswith("text/plain")
    import base64 as _b64

    assert _b64.b64decode(resp.data).decode().count("vless://") >= 1


def test_browser_with_ua_query_gets_config_not_html(http_client, user_three_keys):
    import base64 as _b64

    token = user_three_keys

    resp = http_client.get(
        f"/api/sub/u/{token}?ua=v2ray",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36"},
    )
    assert resp.status_code == 200
    assert resp.mimetype.startswith("text/plain")
    assert _b64.b64decode(resp.data).decode().count("vless://") >= 1


def test_browser_with_ua_clash_gets_yaml(http_client, user_three_keys):
    token = user_three_keys
    resp = http_client.get(
        f"/api/sub/u/{token}?ua=clash",
        headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15"},
    )
    assert resp.status_code == 200
    assert resp.mimetype in ("text/yaml", "application/x-yaml", "text/x-yaml")
    body = resp.get_data(as_text=True)
    assert "proxies:" in body
