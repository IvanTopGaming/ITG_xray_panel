import base64
import json
from copy import deepcopy

import pytest
import yaml
from flask import Flask

from panel_core.api import subscription
from panel_core.extensions import db, limiter
from panel_core.models import LinkedPanel, TelegramUser
from panel_core.services import panel_proxy, sub_cache
from panel_core.services.share_links import build_share_links


CLIENT_ID = "73d16a6e-4a7f-4bed-aaf9-0cac94bf7991"
TOKEN = "review-token"
EXPIRY = 4102444800000


def node_snapshot(*, client_id=CLIENT_ID, enabled=True, expiry=EXPIRY, email="client"):
    return {
        "inbounds": [
            {
                "tag": "shared",
                "label": "Node",
                "protocol": "vless",
                "port": 443,
                "stream_settings": {"network": "tcp", "security": "none"},
                "clients": [
                    {
                        "id": client_id,
                        "email": email,
                        "telegram_id": 123,
                        "enable": enabled,
                        "expiry_time": expiry,
                        "up": 1,
                        "down": 2,
                        "limit_bytes": 1024,
                        "flow": "",
                    }
                ],
            }
        ]
    }


class SnapshotRedis:
    def __init__(self):
        self.snapshots = {1: node_snapshot()}
        self.fail = False
        self.reads = []

    def get(self, key):
        self.reads.append(key)
        if self.fail:
            raise ConnectionError("injected snapshot storage outage")
        parts = key.split(":")
        if len(parts) == 3 and parts[2] == "snapshot":
            snapshot = self.snapshots.get(int(parts[1]))
            return json.dumps(snapshot) if snapshot is not None else None
        return None


@pytest.fixture
def subscription_app(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        RATELIMIT_ENABLED=False,
    )
    db.init_app(app)
    limiter.init_app(app)
    app.register_blueprint(subscription.bp, url_prefix="/api")
    redis = SnapshotRedis()
    cache_writes = []
    monkeypatch.setattr(panel_proxy, "get_shared_redis", lambda: redis)
    monkeypatch.setattr(sub_cache, "get", lambda *args: None)
    monkeypatch.setattr(sub_cache, "set", lambda *args: cache_writes.append(args))
    with app.app_context():
        db.create_all()
        db.session.add(TelegramUser(telegram_id=123, sub_token=TOKEN, language="ru"))
        db.session.add(
            LinkedPanel(id=1, name="First", url="https://one.example", federation_token="fixture", created_at=1)
        )
        db.session.commit()
    yield app, redis, cache_writes
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize("ua", ["v2ray", "clash", "sing-box"])
def test_snapshot_outage_is_retryable_not_expired(subscription_app, ua):
    app, redis, cache_writes = subscription_app
    redis.fail = True
    response = app.test_client().get(f"/api/sub/u/{TOKEN}?ua={ua}")
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert int(response.headers["Retry-After"]) > 0
    assert "Подписка закончилась" not in response.get_data(as_text=True)
    assert cache_writes == []


def test_unknown_token_remains_unknown_during_outage(subscription_app):
    app, redis, _ = subscription_app
    redis.fail = True
    assert app.test_client().get("/api/sub/u/unknown?ua=v2ray").status_code == 404


def test_snapshot_recovery_restores_same_key(subscription_app):
    app, redis, _ = subscription_app
    client = app.test_client()
    initial = client.get(f"/api/sub/u/{TOKEN}?ua=v2ray")
    redis.fail = True
    failed = client.get(f"/api/sub/u/{TOKEN}?ua=v2ray")
    redis.fail = False
    recovered = client.get(f"/api/sub/u/{TOKEN}?ua=v2ray")
    assert [initial.status_code, failed.status_code, recovered.status_code] == [200, 503, 200]
    assert initial.data == recovered.data
    assert CLIENT_ID in base64.b64decode(recovered.data).decode()


@pytest.mark.parametrize("ua", ["v2ray", "clash", "sing-box"])
def test_partial_snapshot_outage_does_not_cache_incomplete_document(subscription_app, ua):
    app, _, cache_writes = subscription_app
    with app.app_context():
        db.session.add(
            LinkedPanel(id=2, name="Second", url="https://two.example", federation_token="fixture", created_at=2)
        )
        db.session.commit()
    response = app.test_client().get(f"/api/sub/u/{TOKEN}?ua={ua}")
    assert response.status_code == 503
    assert cache_writes == []


def test_request_uses_one_snapshot_for_headers_and_body(subscription_app):
    app, redis, _ = subscription_app
    response = app.test_client().get(f"/api/sub/u/{TOKEN}?ua=v2ray")
    assert response.status_code == 200
    assert redis.reads.count("panel:1:snapshot") == 1
    assert "expire=4102444800" in response.headers["subscription-userinfo"]


def test_uuid_unicode_headers_can_be_written_by_http_server(subscription_app):
    app, redis, _ = subscription_app
    redis.snapshots[1] = node_snapshot(email="Пользователь 🔥")
    response = app.test_client().get(f"/api/sub/{CLIENT_ID}?ua=v2ray")
    assert response.status_code == 200
    for value in response.headers.values():
        value.encode("latin-1")
    title = response.headers["profile-title"]
    assert title.startswith("base64:")
    assert base64.b64decode(title.removeprefix("base64:")).decode() == "Пользователь 🔥"


def test_null_expiry_does_not_override_known_finite_expiry(subscription_app):
    app, redis, _ = subscription_app
    second = deepcopy(redis.snapshots[1]["inbounds"][0]["clients"][0])
    second.update(id="894dc604-ed98-4155-9382-422caed1fbb8", expiry_time=None)
    redis.snapshots[1]["inbounds"][0]["clients"].append(second)
    response = app.test_client().get(f"/api/sub/u/{TOKEN}?ua=v2ray")
    assert response.status_code == 200
    assert "expire=4102444800" in response.headers["subscription-userinfo"]


@pytest.mark.parametrize("ua", ["clash", "sing-box"])
def test_different_nodes_with_equal_labels_are_retained(subscription_app, ua):
    app, redis, _ = subscription_app
    with app.app_context():
        db.session.add(
            LinkedPanel(id=2, name="Second", url="https://two.example", federation_token="fixture", created_at=2)
        )
        db.session.commit()
    redis.snapshots[2] = node_snapshot()
    response = app.test_client().get(f"/api/sub/u/{TOKEN}?ua={ua}")
    assert response.status_code == 200
    if ua == "clash":
        entries = yaml.safe_load(response.data)["proxies"]
        assert len({entry["name"] for entry in entries}) == 2
    else:
        entries = [entry for entry in response.json["outbounds"] if "server" in entry]
        assert len({entry["tag"] for entry in entries}) == 2
    assert {entry["server"] for entry in entries} == {"one.example", "two.example"}


def test_unrelated_corrupt_inbound_does_not_hide_matching_uuid(subscription_app):
    app, redis, _ = subscription_app
    redis.snapshots[1]["inbounds"].insert(0, {"tag": "broken", "clients": None})
    response = app.test_client().get(f"/api/sub/{CLIENT_ID}?ua=v2ray")
    assert response.status_code == 200
    assert CLIENT_ID in base64.b64decode(response.data).decode()


def test_subscription_page_does_not_report_absence_on_snapshot_outage(subscription_app):
    app, redis, _ = subscription_app
    redis.fail = True
    response = app.test_client().get(f"/api/sub/u/{TOKEN}/info")
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("route", [f"/api/sub/{CLIENT_ID}", f"/api/sub/u/{TOKEN}"])
def test_disabled_live_term_is_not_described_as_expired(subscription_app, route):
    app, redis, _ = subscription_app
    redis.snapshots[1] = node_snapshot(enabled=False)
    response = app.test_client().get(route + "?ua=v2ray")
    assert response.status_code == 200
    from urllib.parse import unquote

    message = unquote(base64.b64decode(response.data).decode())
    assert "Подписка закончилась" not in message
    assert "отключ" in message.lower()


def test_uuid_does_not_bypass_account_block(subscription_app):
    app, _, _ = subscription_app
    with app.app_context():
        db.session.get(TelegramUser, 123).blocked = True
        db.session.commit()
    response = app.test_client().get(f"/api/sub/{CLIENT_ID}?ua=v2ray")
    assert response.status_code == 200
    assert CLIENT_ID not in base64.b64decode(response.data).decode()


def test_known_blocked_info_is_not_unknown_token(subscription_app):
    app, redis, _ = subscription_app
    with app.app_context():
        db.session.get(TelegramUser, 123).blocked = True
        db.session.commit()
    redis.fail = True
    response = app.test_client().get(f"/api/sub/u/{TOKEN}/info")
    assert response.status_code == 200
    assert response.json["status"] == "disabled"
    assert response.json["reason"] == "blocked"


@pytest.mark.parametrize("ua", ["v2ray", "clash", "sing-box"])
def test_expired_but_not_enforced_key_is_not_served(subscription_app, ua):
    app, redis, _ = subscription_app
    redis.snapshots[1] = node_snapshot(expiry=1)
    response = app.test_client().get(f"/api/sub/u/{TOKEN}?ua={ua}")
    assert response.status_code == 200
    assert CLIENT_ID not in response.get_data(as_text=True)
    if ua == "v2ray":
        assert CLIENT_ID not in base64.b64decode(response.data).decode()


def test_uuid_unknown_expiry_does_not_claim_unlimited(subscription_app):
    app, redis, _ = subscription_app
    redis.snapshots[1] = node_snapshot(expiry=None)
    response = app.test_client().get(f"/api/sub/{CLIENT_ID}?ua=v2ray")
    assert response.status_code == 200
    assert "expire=" not in response.headers["subscription-userinfo"]


def test_mihomo_xhttp_is_not_http_transport(subscription_app):
    app, redis, _ = subscription_app
    redis.snapshots[1]["inbounds"][0]["stream_settings"] = {
        "network": "xhttp",
        "security": "none",
        "xhttpSettings": {"path": "/edge", "host": "cdn.example", "mode": "stream-up"},
    }
    response = app.test_client().get(f"/api/sub/u/{TOKEN}?ua=clash")
    assert response.status_code == 200
    proxy = yaml.safe_load(response.data)["proxies"][0]
    assert proxy["network"] == "xhttp"
    assert proxy["xhttp-opts"] == {"path": "/edge", "host": "cdn.example", "mode": "stream-up"}


@pytest.mark.parametrize("network", ["kcp", "domainsocket", "xhttp", "splithttp"])
def test_unsupported_singbox_transport_is_explicit(subscription_app, network):
    app, redis, cache_writes = subscription_app
    redis.snapshots[1]["inbounds"][0]["stream_settings"] = {"network": network, "security": "none"}
    response = app.test_client().get(f"/api/sub/u/{TOKEN}?ua=sing-box")
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert "unsupported" in response.get_data(as_text=True).lower()
    assert cache_writes == []


@pytest.mark.parametrize("protocol", ["vless", "trojan", "shadowsocks"])
def test_raw_ipv6_link_preserves_hostname_and_port(protocol):
    from urllib.parse import urlsplit

    link = build_share_links(
        "2001:db8::1", protocol, 8443, {"network": "tcp", "security": "none"}, CLIENT_ID, "", "node"
    )[0]
    parsed = urlsplit(link)
    assert parsed.hostname == "2001:db8::1"
    assert parsed.port == 8443


@pytest.mark.parametrize("ua", ["v2ray", "clash", "sing-box"])
def test_cache_does_not_reuse_previous_snapshot_generation(subscription_app, monkeypatch, ua):
    app, redis, _ = subscription_app
    store = {}
    monkeypatch.setattr(sub_cache, "get", lambda kind, key: store.get((kind, key)))
    monkeypatch.setattr(sub_cache, "set", lambda kind, key, value: store.__setitem__((kind, key), value))
    client = app.test_client()
    initial = client.get(f"/api/sub/u/{TOKEN}?ua={ua}")
    replacement = "894dc604-ed98-4155-9382-422caed1fbb8"
    redis.snapshots[1] = node_snapshot(client_id=replacement)
    current = client.get(f"/api/sub/u/{TOKEN}?ua={ua}")
    assert initial.status_code == current.status_code == 200
    body = base64.b64decode(current.data).decode() if ua == "v2ray" else current.get_data(as_text=True)
    assert replacement in body
    assert CLIENT_ID not in body


def test_cache_does_not_keep_expired_member_beside_active_one(subscription_app, monkeypatch):
    app, redis, _ = subscription_app
    store = {}
    monkeypatch.setattr(sub_cache, "get", lambda kind, key: store.get((kind, key)))
    monkeypatch.setattr(sub_cache, "set", lambda kind, key, value: store.__setitem__((kind, key), value))
    second = deepcopy(redis.snapshots[1]["inbounds"][0]["clients"][0])
    second["id"] = "894dc604-ed98-4155-9382-422caed1fbb8"
    redis.snapshots[1]["inbounds"][0]["clients"].append(second)
    client = app.test_client()
    assert client.get(f"/api/sub/u/{TOKEN}?ua=v2ray").status_code == 200
    redis.snapshots[1]["inbounds"][0]["clients"][0]["expiry_time"] = 1
    current = client.get(f"/api/sub/u/{TOKEN}?ua=v2ray")
    body = base64.b64decode(current.data).decode()
    assert second["id"] in body
    assert CLIENT_ID not in body


def _bot_client(app):
    from panel_core.api import bot_service
    from panel_core.models import SystemSetting

    app.register_blueprint(bot_service.bp, url_prefix="/api")
    with app.app_context():
        db.session.add(SystemSetting(key="bot_service_token", value="review-service"))
        db.session.commit()
    return app.test_client(), {"Authorization": "Bearer review-service"}


def test_bot_state_does_not_report_no_subscription_on_outage(subscription_app):
    app, redis, _ = subscription_app
    client, headers = _bot_client(app)
    redis.fail = True
    response = client.get("/api/bot-service/users/123/state", headers=headers)
    assert response.status_code == 503
    assert response.json["error"] == "subscription_unavailable"
    assert response.headers["Cache-Control"] == "no-store"


def test_bot_state_blocked_account_does_not_expose_keys(subscription_app):
    app, redis, _ = subscription_app
    client, headers = _bot_client(app)
    with app.app_context():
        db.session.get(TelegramUser, 123).blocked = True
        db.session.commit()
    response = client.get("/api/bot-service/users/123/state", headers=headers)
    assert response.status_code == 200
    assert response.json["blocked"] is True
    assert response.json["clients"] == []
    assert redis.reads == []


def test_bot_state_unknown_expiry_stays_unknown(subscription_app):
    app, redis, _ = subscription_app
    client, headers = _bot_client(app)
    redis.snapshots[1] = node_snapshot(expiry=None)
    response = client.get("/api/bot-service/users/123/state", headers=headers)
    assert response.status_code == 200
    assert response.json["expires_at_ms"] is None


def test_bot_state_has_the_actual_trial_duration(subscription_app):
    app, _, _ = subscription_app
    client, headers = _bot_client(app)
    from panel_core.models import Tariff, TariffItem

    with app.app_context():
        trial = Tariff(name="Three day trial", period_days=3, price_rub=0, enabled=True, is_trial=True)
        db.session.add(trial)
        db.session.flush()
        db.session.add(TariffItem(tariff_id=trial.id, panel_id=1, inbound_tag="shared", traffic_gb=0))
        db.session.commit()
    response = client.get("/api/bot-service/users/123/state", headers=headers)
    assert response.status_code == 200
    assert response.json["trial_available"] is True
    assert response.json["trial_days"] == 3
