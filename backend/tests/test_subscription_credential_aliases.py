import base64
import json

import pytest
import yaml

from panel_core.api import subscription
from panel_core.extensions import db
from panel_core.models import LinkedPanel, TelegramUser
from panel_core.services import panel_proxy, sub_cache


CANONICAL = "11111111-1111-4111-8111-111111111111"
ALIAS = "22222222-2222-4222-8222-222222222222"
TOKEN = "credential-alias-subscription"


@pytest.fixture
def subscription_case(app, monkeypatch):
    app.register_blueprint(subscription.bp, url_prefix="/api")
    cache = {}
    monkeypatch.setattr(sub_cache, "get", lambda kind, key: cache.get((kind, key)))
    monkeypatch.setattr(sub_cache, "set", lambda kind, key, body: cache.__setitem__((kind, key), body))
    snapshot = {
        "inbounds": [
            {
                "tag": "shared",
                "label": "Shared access",
                "protocol": "vless",
                "port": 8443,
                "stream_settings": {"network": "tcp", "security": "none"},
                "clients": [
                    {
                        "id": CANONICAL,
                        "email": "canonical",
                        "telegram_id": 123,
                        "enable": True,
                        "expiry_time": 4102444800000,
                        "up": 11,
                        "down": 22,
                        "limit_bytes": 1024,
                        "flow": "",
                        "credential_aliases": [{"id": ALIAS, "email": "old", "flow": "", "wg_address": None}],
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(panel_proxy, "get_panel_snapshot", lambda panel_id: snapshot)
    db.session.add(TelegramUser(telegram_id=123, sub_token=TOKEN))
    db.session.add(LinkedPanel(id=1, name="node", url="https://node.example", federation_token="test", created_at=1))
    db.session.commit()
    return app.test_client(), snapshot, cache


def credentials(response, format):
    if format == "v2ray":
        links = base64.b64decode(response.data).decode().splitlines()
        return [link.split("://", 1)[1].split("@", 1)[0] for link in links]
    if format == "clash":
        return [proxy["uuid"] for proxy in yaml.safe_load(response.data)["proxies"]]
    return [outbound["uuid"] for outbound in json.loads(response.data)["outbounds"] if "uuid" in outbound]


@pytest.mark.parametrize("format", ["v2ray", "clash", "sing-box"])
def test_old_uuid_subscription_uses_canonical_access_and_shared_counters(subscription_case, format):
    client, _, _ = subscription_case
    response = client.get(f"/api/sub/{ALIAS}?ua={format}")

    assert response.status_code == 200
    assert credentials(response, format) == [CANONICAL]
    assert response.headers["subscription-userinfo"] == "upload=11; download=22; total=1024; expire=4102444800"


@pytest.mark.parametrize("format", ["v2ray", "clash", "sing-box"])
def test_aggregate_subscription_counts_alias_holder_once(subscription_case, format):
    client, _, _ = subscription_case
    response = client.get(f"/api/sub/u/{TOKEN}?ua={format}")

    assert response.status_code == 200
    assert credentials(response, format) == [CANONICAL]
    assert response.headers["subscription-userinfo"] == "upload=11; download=22; total=1024; expire=4102444800"


@pytest.mark.parametrize("field,value", [("enable", False), ("expiry_time", 1), ("down", 1024)])
@pytest.mark.parametrize("local", [False, True])
def test_alias_subscription_cannot_bypass_canonical_restrictions(subscription_case, request, field, value, local):
    client, snapshot, _ = subscription_case
    if local:
        from panel_core.models import Client

        client, _ = request.getfixturevalue("local_subscription_case")
    assert client.get(f"/api/sub/{ALIAS}?ua=v2ray").status_code == 200
    if local:
        setattr(db.session.get(Client, CANONICAL), field, value)
        db.session.commit()
    else:
        snapshot["inbounds"][0]["clients"][0][field] = value

    response = client.get(f"/api/sub/{ALIAS}?ua=v2ray")

    assert response.status_code == 200
    assert credentials(response, "v2ray") == ["00000000-0000-0000-0000-000000000000"]


def test_removed_snapshot_alias_cannot_reuse_cached_subscription(subscription_case):
    client, snapshot, _ = subscription_case
    assert client.get(f"/api/sub/{ALIAS}?ua=v2ray").status_code == 200
    snapshot["inbounds"][0]["clients"][0]["credential_aliases"] = []

    assert client.get(f"/api/sub/{ALIAS}?ua=v2ray").status_code == 404


@pytest.fixture
def local_subscription_case(subscription_case):
    from panel_core.models import Client, ClientCredential, Inbound

    client, snapshot, cache = subscription_case
    inbound = snapshot["inbounds"][0]
    canonical = inbound["clients"][0]
    db.session.add(
        Inbound(
            tag=inbound["tag"],
            protocol=inbound["protocol"],
            port=inbound["port"],
            label=inbound["label"],
            stream_settings=json.dumps(inbound["stream_settings"]),
        )
    )
    db.session.add(
        Client(**{key: value for key, value in canonical.items() if key != "credential_aliases"}, inbound_tag="shared")
    )
    db.session.flush()
    db.session.add(ClientCredential(client_id=CANONICAL, **canonical["credential_aliases"][0]))
    db.session.commit()
    snapshot["inbounds"] = []
    return client, cache


@pytest.mark.parametrize("format", ["v2ray", "clash", "sing-box"])
def test_local_alias_subscription_resolves_canonical_client(local_subscription_case, format):
    client, _ = local_subscription_case
    response = client.get(f"/api/sub/{ALIAS}?ua={format}")

    assert response.status_code == 200
    assert credentials(response, format) == [CANONICAL]
    assert response.headers["subscription-userinfo"] == "upload=11; download=22; total=1024; expire=4102444800"


@pytest.mark.parametrize("format", ["v2ray", "clash", "sing-box"])
def test_local_aggregate_contains_one_entry_per_canonical_client(local_subscription_case, format):
    client, _ = local_subscription_case
    response = client.get(f"/api/sub/u/{TOKEN}?ua={format}")

    assert response.status_code == 200
    assert credentials(response, format) == [CANONICAL]
    assert response.headers["subscription-userinfo"] == "upload=11; download=22; total=1024; expire=4102444800"


def test_removed_local_alias_cannot_reuse_cached_subscription(local_subscription_case):
    from panel_core.models import ClientCredential

    client, _ = local_subscription_case
    assert client.get(f"/api/sub/{ALIAS}?ua=v2ray").status_code == 200
    db.session.delete(db.session.get(ClientCredential, ALIAS))
    db.session.commit()

    assert client.get(f"/api/sub/{ALIAS}?ua=v2ray").status_code == 404


@pytest.mark.parametrize("local", [False, True])
def test_alias_obeys_account_block_even_after_cache_is_filled(subscription_case, request, local):
    if local:
        client, _ = request.getfixturevalue("local_subscription_case")
    else:
        client, _, _ = subscription_case
    assert client.get(f"/api/sub/{ALIAS}?ua=v2ray").status_code == 200
    db.session.get(TelegramUser, 123).blocked = True
    db.session.commit()

    response = client.get(f"/api/sub/{ALIAS}?ua=v2ray")

    assert response.status_code == 200
    assert credentials(response, "v2ray") == ["00000000-0000-0000-0000-000000000000"]
