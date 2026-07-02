import json

import pytest

from app.extensions import db
from app.models import Client, ClientDevice, Inbound, SystemSetting, TelegramUser


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
    from app.api.subscription import _protocol_tag

    assert _protocol_tag("vless", {"network": "tcp", "security": "reality"}) == "Reality"


def test_protocol_tag_vless_ws():
    from app.api.subscription import _protocol_tag

    assert _protocol_tag("vless", {"network": "ws", "security": "tls"}) == "VLESS-WS"


def test_protocol_tag_plain_vless():
    from app.api.subscription import _protocol_tag

    assert _protocol_tag("vless", {"network": "tcp", "security": "tls"}) == "VLESS"


def test_protocol_tag_trojan_vmess_ss():
    from app.api.subscription import _protocol_tag

    assert _protocol_tag("trojan", {"network": "tcp", "security": "tls"}) == "Trojan"
    assert _protocol_tag("vmess", {"network": "ws", "security": "none"}) == "VMess-WS"
    assert _protocol_tag("shadowsocks", {}) == "Shadowsocks"


def test_protocol_tag_accepts_json_string():
    from app.api.subscription import _protocol_tag

    assert _protocol_tag("vless", REALITY_STREAM) == "Reality"


@pytest.fixture
def app(app):
    from app.api import subscription as sub_api

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
    from app.api.subscription import _user_page_nodes

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
    from app.api.subscription import _user_page_nodes

    with app.app_context():
        assert _user_page_nodes(999999) == []


def test_device_summary_hidden_without_limit(app, user_three_keys):
    from app.api.subscription import _user_device_summary

    with app.app_context():
        assert _user_device_summary(800) is None


def test_device_summary_counts_unique_hwids(app, user_three_keys):
    import time

    from app.api.subscription import _user_device_summary

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


def test_pick_lang_query_overrides_header(app):
    from app.api.subscription import _pick_lang

    assert _pick_lang("ru", "en-US,en;q=0.9") == "ru"
    assert _pick_lang("EN", "ru") == "en"
    assert _pick_lang("fr", "ru") == "en"


def test_pick_lang_accept_language_fallback(app):
    from app.api.subscription import _pick_lang

    assert _pick_lang("", "ru-RU,ru;q=0.9,en;q=0.8") == "ru"
    assert _pick_lang("", "en-GB,en;q=0.9") == "en"
    assert _pick_lang("", "") == "en"


def test_page_strings_have_both_langs(app):
    from app.api.subscription import _PAGE_STRINGS

    assert set(_PAGE_STRINGS.keys()) == {"en", "ru"}
    assert set(_PAGE_STRINGS["en"].keys()) == set(_PAGE_STRINGS["ru"].keys())


def test_render_page_contains_nodes_and_brand(app, user_three_keys):
    from app.api.subscription import render_aggregate_subscription_page

    with app.app_context():
        db.session.add(SystemSetting(key="brand_name", value="ACME VPN"))
        db.session.commit()
        user = TelegramUser.query.filter_by(telegram_id=800).first()
        html_doc = render_aggregate_subscription_page(user, "en", "https://sub.example.com/api/sub/u/" + user.sub_token)

        assert html_doc is not None
        assert "ACME VPN" in html_doc
        assert "🇳🇱 Amsterdam" in html_doc
        assert "🇩🇪 Frankfurt" in html_doc
        assert "🇫🇮 Helsinki" in html_doc
        assert "Reality" in html_doc and "Trojan" in html_doc
        assert "node disabled" in html_doc
        assert "Unlimited" in html_doc
        assert "https://sub.example.com/api/sub/u/" + user.sub_token in html_doc
        assert "Copy link" in html_doc
        assert "Devices" not in html_doc


def test_render_page_ru_and_devices_card(app, user_three_keys):
    import time

    from app.api.subscription import render_aggregate_subscription_page

    now_ms = int(time.time() * 1000)
    with app.app_context():
        db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
        db.session.add(SystemSetting(key="device_limit_per_user", value="2"))
        db.session.add(ClientDevice(client_id="c1", hwid="hw-X", first_seen=now_ms, last_seen=now_ms))
        db.session.commit()
        user = TelegramUser.query.filter_by(telegram_id=800).first()
        html_doc = render_aggregate_subscription_page(user, "ru", "https://sub.example.com/api/sub/u/" + user.sub_token)
        assert "Подписка" in html_doc
        assert "Устройства" in html_doc
        assert "Узлы" in html_doc


@pytest.fixture
def http_client(app):
    return app.test_client()


def test_browser_gets_html_page(http_client, user_three_keys):
    token = user_three_keys
    resp = http_client.get(
        f"/api/sub/u/{token}",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    assert "🇳🇱 Amsterdam" in body
    assert "<!doctype html>" in body.lower()


def test_browser_lang_query(http_client, user_three_keys):
    token = user_three_keys
    resp = http_client.get(
        f"/api/sub/u/{token}?lang=ru",
        headers={"User-Agent": "Mozilla/5.0 AppleWebKit/537.36"},
    )
    body = resp.get_data(as_text=True)
    assert "Узлы" in body


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


def test_format_date_localized_ru_en(app):
    import time as _t

    from app.api.subscription import _format_date_localized

    ms = int(_t.mktime(_t.struct_time((2026, 6, 18, 12, 0, 0, 0, 0, -1))) * 1000)
    assert _format_date_localized(ms, "ru") == "18 июня"
    assert _format_date_localized(ms, "en") == "18 June"
    assert _format_date_localized(0, "ru") == "бессрочно"


def test_page_days_left_rounds_up_and_localized_date(app, user_three_keys):
    import re

    from app.api.subscription import render_aggregate_subscription_page, _format_date_localized
    from app.models import Client, TelegramUser

    with app.app_context():
        user = TelegramUser.query.filter_by(telegram_id=800).first()
        expected_date = _format_date_localized(Client.query.filter_by(id="c1").first().expiry_time, "ru")
        html_doc = render_aggregate_subscription_page(user, "ru", "https://s/api/sub/u/x")
        m = re.search(r"осталось (\d+) дн", html_doc)
        assert m and int(m.group(1)) == 12
        assert expected_date in html_doc
        assert re.search(r"[а-я]{3,}", expected_date)
        assert not re.search(r"\d{4}-\d{2}-\d{2}", html_doc)


def test_render_page_escapes_brand_and_shows_offline_dot(app, user_three_keys):
    from app.api.subscription import render_aggregate_subscription_page
    from app.models import SystemSetting, TelegramUser

    with app.app_context():
        db.session.add(SystemSetting(key="brand_name", value="<script>x</script>"))
        db.session.commit()
        user = TelegramUser.query.filter_by(telegram_id=800).first()
        html_doc = render_aggregate_subscription_page(user, "en", "https://s/api/sub/u/x")
        assert "<script>x</script>" not in html_doc
        assert "&lt;script&gt;" in html_doc
        assert "dot off" in html_doc


def test_render_page_expired_node(app):
    import time

    from app.api.subscription import render_aggregate_subscription_page
    from app.models import Client, Inbound, TelegramUser

    now = int(time.time() * 1000)
    with app.app_context():
        db.session.add(Inbound(tag="ex", port=20100, protocol="vless", stream_settings=REALITY_STREAM, label="EX"))
        u = TelegramUser(telegram_id=850, sub_token="tok-850-expiredaaaaaaaaaaaaaaaaaaa")
        db.session.add(u)
        db.session.add(
            Client(
                id="e1",
                email="u850",
                inbound_tag="ex",
                telegram_id=850,
                enable=True,
                up=0,
                down=0,
                limit_bytes=0,
                expiry_time=now - 86400_000,
                last_seen=now,
            )
        )
        db.session.commit()
        html_doc = render_aggregate_subscription_page(u, "ru", "https://s/api/sub/u/x")
        assert "истекла" in html_doc
