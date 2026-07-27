import json
import time
import uuid

import pytest

from panel_core.extensions import db
from panel_core.models import Client, Inbound, SystemSetting, TelegramUser


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

HOUR_MS = 3600 * 1000


@pytest.fixture
def app(app):
    from panel_core.api import subscription as sub_api

    if "subscription" not in app.blueprints:
        app.register_blueprint(sub_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _seed(app, *, blocked=False, clients=(), devices_enabled=False, device_limit=0):
    with app.app_context():
        user = TelegramUser(telegram_id=777, sub_token="tok777", language="ru", blocked=blocked)
        db.session.add(user)
        inbound = Inbound(
            tag="vless-reality",
            label="Нидерланды",
            protocol="vless",
            port=443,
            stream_settings=REALITY_STREAM,
        )
        db.session.add(inbound)
        for spec in clients:
            db.session.add(
                Client(
                    id=spec.get("id", str(uuid.uuid4())),
                    email=spec["email"],
                    inbound_tag="vless-reality",
                    telegram_id=777,
                    enable=spec.get("enable", True),
                    up=spec.get("up", 0),
                    down=spec.get("down", 0),
                    limit_bytes=spec.get("limit_bytes", 0),
                    expiry_time=spec.get("expiry_time", 0),
                )
            )
        if devices_enabled:
            db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
            db.session.add(SystemSetting(key="device_limit_per_user", value=str(device_limit)))
        db.session.commit()


def test_info_returns_active_status_and_raw_numbers(app, client):
    expiry = int(time.time() * 1000) + 48 * HOUR_MS
    _seed(
        app,
        clients=[{"email": "tg777_vless", "up": 1024, "down": 2048, "limit_bytes": 4096, "expiry_time": expiry}],
    )

    response = client.get("/api/sub/u/tok777/info")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "active"
    assert body["expiry_at"] == expiry
    assert body["nodes"] == [
        {
            "name": "Нидерланды",
            "tag": "Reality",
            "used": 3072,
            "limit": 4096,
            "expiry": expiry,
            "online": False,
            "enabled": True,
        }
    ]
    assert body["update_interval_hours"] == 24
    assert body["devices"] is None
    assert "unlimited" not in body["nodes"][0]


def test_info_reports_disabled_when_every_node_is_off(app, client):
    _seed(app, clients=[{"email": "tg777_vless", "enable": False}])

    body = client.get("/api/sub/u/tok777/info").get_json()

    assert body["status"] == "disabled"
    assert body["nodes"][0]["enabled"] is False


def test_info_reports_disabled_with_no_nodes_at_all(app, client):
    _seed(app)

    body = client.get("/api/sub/u/tok777/info").get_json()

    assert body["status"] == "disabled"
    assert body["nodes"] == []
    assert body["expiry_at"] == 0


def test_info_keeps_an_unlimited_node_as_a_zero_limit(app, client):
    _seed(app, clients=[{"email": "tg777_vless", "up": 5, "down": 5, "limit_bytes": 0}])

    body = client.get("/api/sub/u/tok777/info").get_json()

    assert body["nodes"][0]["limit"] == 0
    assert body["nodes"][0]["used"] == 10


def test_info_reports_device_counters_when_the_limit_is_enabled(app, client):
    _seed(app, clients=[{"email": "tg777_vless"}], devices_enabled=True, device_limit=5)

    body = client.get("/api/sub/u/tok777/info").get_json()

    assert body["devices"] == {"count": 0, "limit": 5}


def test_info_404s_on_an_unknown_token(app, client):
    _seed(app, clients=[{"email": "tg777_vless"}])

    assert client.get("/api/sub/u/nope/info").status_code == 404


def test_info_404s_on_a_blocked_user_without_leaking_a_body(app, client):
    _seed(app, blocked=True, clients=[{"email": "tg777_vless"}])

    response = client.get("/api/sub/u/tok777/info")

    assert response.status_code == 404
    assert b"\xd0\x9d\xd0\xb8\xd0\xb4\xd0\xb5\xd1\x80\xd0\xbb\xd0\xb0\xd0\xbd\xd0\xb4\xd1\x8b" not in response.data
    assert b"vless-reality" not in response.data


SUB_URL_DOC = (
    "sub_url is what the page turns into a QR and into three one-tap deep links, so a link that "
    "404s is no longer a string a human reads in a copy box -- it is an unscannable QR and three "
    "buttons that import a dead subscription. On a PANEL_DOMAIN-only install the page is reachable "
    "only under /<PANEL_SECRET_PATH>/api/..., and caddygen strips that prefix without sending "
    "X-Forwarded-Prefix, so the request headers cannot reconstruct it. services/sub_links."
    "build_aggregate_sub_url is the one place that knows the rule; _absolute_sub_url must delegate "
    "to it rather than re-deriving the URL from the request."
)


def test_info_prefers_the_sub_domain_for_the_link(app, client, monkeypatch):
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.ru")
    _seed(app, clients=[{"email": "tg777_vless"}])

    body = client.get("/api/sub/u/tok777/info").get_json()

    assert body["sub_url"] == "https://sub.example.ru/api/sub/u/tok777"


def test_info_link_keeps_the_sub_domain_clean_even_when_a_secret_path_is_set(app, client, monkeypatch):
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.ru")
    monkeypatch.setenv("PANEL_DOMAIN", "panel.example.ru")
    monkeypatch.setenv("PANEL_SECRET_PATH", "s3cr3t")
    _seed(app, clients=[{"email": "tg777_vless"}])

    body = client.get("/api/sub/u/tok777/info").get_json()

    assert body["sub_url"] == "https://sub.example.ru/api/sub/u/tok777", SUB_URL_DOC


def test_info_link_carries_the_secret_path_when_there_is_no_sub_domain(app, client, monkeypatch):
    monkeypatch.setenv("SUB_DOMAIN", "")
    monkeypatch.setenv("PANEL_DOMAIN", "panel.example.ru")
    monkeypatch.setenv("PANEL_SECRET_PATH", "s3cr3t")
    _seed(app, clients=[{"email": "tg777_vless"}])

    body = client.get("/api/sub/u/tok777/info").get_json()

    assert body["sub_url"] == "https://panel.example.ru/s3cr3t/api/sub/u/tok777", SUB_URL_DOC


def test_info_link_uses_the_bare_panel_domain_when_no_secret_path_is_set(app, client, monkeypatch):
    monkeypatch.setenv("SUB_DOMAIN", "")
    monkeypatch.setenv("PANEL_DOMAIN", "panel.example.ru")
    monkeypatch.setenv("PANEL_SECRET_PATH", "")
    _seed(app, clients=[{"email": "tg777_vless"}])

    body = client.get("/api/sub/u/tok777/info").get_json()

    assert body["sub_url"] == "https://panel.example.ru/api/sub/u/tok777", SUB_URL_DOC


def test_info_link_falls_back_to_the_request_when_no_domain_is_configured(app, client, monkeypatch):
    monkeypatch.setenv("SUB_DOMAIN", "")
    monkeypatch.setenv("PANEL_DOMAIN", "")
    monkeypatch.setenv("PANEL_SECRET_PATH", "")
    _seed(app, clients=[{"email": "tg777_vless"}])

    body = client.get(
        "/api/sub/u/tok777/info",
        headers={"X-Forwarded-Host": "fallback.example.ru", "X-Forwarded-Proto": "https"},
    ).get_json()

    assert body["sub_url"] == "https://fallback.example.ru/api/sub/u/tok777", (
        "build_aggregate_sub_url returns None when PANEL_DOMAIN is unset, and None must never reach "
        f"the payload -- the request-derived form stays as the fallback for that case.\n\n{SUB_URL_DOC}"
    )


def test_info_picks_the_nearest_expiry_among_enabled_nodes(app, client):
    now = int(time.time() * 1000)
    earliest_disabled = now + 1 * HOUR_MS
    earliest_enabled = now + 2 * HOUR_MS
    later_enabled = now + 3 * HOUR_MS
    _seed(
        app,
        clients=[
            {"email": "tg777_vless_a", "enable": False, "expiry_time": earliest_disabled},
            {"email": "tg777_vless_b", "enable": True, "expiry_time": earliest_enabled},
            {"email": "tg777_vless_c", "enable": True, "expiry_time": later_enabled},
        ],
    )

    body = client.get("/api/sub/u/tok777/info").get_json()

    assert body["expiry_at"] == earliest_enabled


def test_info_falls_back_to_the_nearest_expiry_when_every_node_is_disabled(app, client):
    now = int(time.time() * 1000)
    earliest_disabled = now + 1 * HOUR_MS
    later_disabled = now + 5 * HOUR_MS
    _seed(
        app,
        clients=[
            {"email": "tg777_vless_a", "enable": False, "expiry_time": earliest_disabled},
            {"email": "tg777_vless_b", "enable": False, "expiry_time": later_disabled},
        ],
    )

    body = client.get("/api/sub/u/tok777/info").get_json()

    assert body["expiry_at"] == earliest_disabled
