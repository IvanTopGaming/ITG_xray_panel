from types import SimpleNamespace

import pytest

from panel_core.models import AccessEntitlement, Client, Inbound, Tariff, TariffItem
from panel_core.services.provisioning import _collect_tariff_holders
from panel_core.services.remote_clients import _bucket_panel_clients


@pytest.fixture
def shared_client(db):
    db.session.add(Inbound(tag="edge", protocol="vless", port=12345, stream_settings="{}"))
    first = Tariff(name="Large", price_rub=10, period_days=30)
    second = Tariff(name="Small", price_rub=5, period_days=30)
    db.session.add_all([first, second])
    db.session.flush()
    client = Client(
        id="shared",
        email="shared",
        inbound_tag="edge",
        telegram_id=42,
        tariff_id=first.id,
        expiry_time=9000,
        enable=True,
        active_entitlement_source="large",
        provisioning_key="tg:42:edge",
        up=30,
        down=40,
    )
    db.session.add(client)
    db.session.flush()
    db.session.add_all(
        [
            AccessEntitlement(
                source_id="large",
                operation_id="large",
                telegram_id=42,
                tariff_id=first.id,
                inbound_tag="edge",
                client_id=client.id,
                expires_at_ms=5000,
                limit_bytes=100,
                up=1,
                down=2,
            ),
            AccessEntitlement(
                source_id="small",
                operation_id="small",
                telegram_id=42,
                tariff_id=second.id,
                inbound_tag="edge",
                client_id=client.id,
                expires_at_ms=9000,
                limit_bytes=10,
            ),
            AccessEntitlement(
                source_id="old",
                operation_id="old",
                telegram_id=42,
                tariff_id=first.id,
                inbound_tag="edge",
                client_id=client.id,
                expires_at_ms=500,
                limit_bytes=100,
                revoked=True,
            ),
        ]
    )
    db.session.commit()
    return client, first, second


def test_client_snapshot_preserves_all_sources_and_live_selected_usage(db, shared_client):
    client, first, second = shared_client
    sources = client.to_dict().get("tariff_sources")
    assert sources == [
        {
            "source_id": "large",
            "tariff_id": first.id,
            "expires_at_ms": 5000,
            "limit_bytes": 100,
            "up": 30,
            "down": 40,
            "enabled": True,
            "revoked": False,
        },
        {
            "source_id": "small",
            "tariff_id": second.id,
            "expires_at_ms": 9000,
            "limit_bytes": 10,
            "up": 0,
            "down": 0,
            "enabled": True,
            "revoked": False,
        },
        {
            "source_id": "old",
            "tariff_id": first.id,
            "expires_at_ms": 500,
            "limit_bytes": 100,
            "up": 0,
            "down": 0,
            "enabled": True,
            "revoked": True,
        },
    ]
    bucket = {}
    _bucket_panel_clients(
        bucket, {"inbounds": [{"tag": "edge", "clients": [client.to_dict()]}]}, SimpleNamespace(id=3, name="Node")
    )
    assert bucket[42][0]["tariff_sources"] == sources


def test_backfill_finds_non_selected_tariff_with_its_own_expiry(db, shared_client):
    _, first, second = shared_client
    assert _collect_tariff_holders(first, 1000)[0] == {42: {"expiry_ms": 5000, "have": {(None, "edge")}}}
    assert _collect_tariff_holders(second, 1000)[0] == {42: {"expiry_ms": 9000, "have": {(None, "edge")}}}
    assert _collect_tariff_holders(first, 5000)[0] == {}


@pytest.mark.parametrize(
    "revoked,enabled,expiry,expected",
    [
        (False, True, 5000, 5000),
        (False, True, 0, 0),
        (True, True, 5000, None),
        (False, False, 5000, None),
        (False, True, 500, None),
    ],
)
def test_remote_backfill_uses_source_membership(db, monkeypatch, shared_client, revoked, enabled, expiry, expected):
    client, first, second = shared_client
    db.session.add(TariffItem(tariff_id=second.id, inbound_tag="remote", panel_id=3, traffic_gb=1))
    db.session.delete(client)
    db.session.commit()
    snapshot = {
        "inbounds": [
            {
                "tag": "remote",
                "clients": [
                    {
                        "telegram_id": 77,
                        "tariff_id": first.id,
                        "expiry_time": 9000,
                        "enable": True,
                        "tariff_sources": [
                            {
                                "source_id": "remote",
                                "tariff_id": second.id,
                                "expires_at_ms": expiry,
                                "enabled": enabled,
                                "revoked": revoked,
                            }
                        ],
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr("panel_core.services.provisioning.fetch_panel_snapshot_live", lambda _: snapshot)
    holders, failures = _collect_tariff_holders(second, 1000)
    assert holders == ({} if expected is None else {77: {"expiry_ms": expected, "have": {(3, "remote")}}})
    assert failures == set()


def test_snapshot_preserves_legacy_credentials_without_a_second_client(db, shared_client):
    from panel_core.models import ClientCredential

    client, _, _ = shared_client
    db.session.add(ClientCredential(id="old-key", client_id=client.id, email="old-email", flow="xtls-rprx-vision"))
    db.session.commit()
    expected = [{"id": "old-key", "email": "old-email", "flow": "xtls-rprx-vision", "wg_address": None}]
    assert client.to_dict().get("credential_aliases") == expected
    bucket = {}
    _bucket_panel_clients(
        bucket, {"inbounds": [{"tag": "edge", "clients": [client.to_dict()]}]}, SimpleNamespace(id=3, name="Node")
    )
    assert bucket[42][0].get("credential_aliases") == expected


def test_tariff_revoke_discovers_non_owner_source_after_item_removal(db, shared_client, monkeypatch):
    from panel_core.services import entitlements, grants

    client, first, second = shared_client
    for source in AccessEntitlement.query.all():
        source.expires_at_ms = 4102444800000
    client.expiry_time = 4102444800000
    db.session.commit()
    monkeypatch.setattr(entitlements, "_sync", lambda *args: None)
    monkeypatch.setattr(entitlements, "settle_client_traffic", lambda *args, **kwargs: (0, 0))
    result = grants.revoke_tariff(42, second.id)
    assert result["panel_failures"] == []
    assert AccessEntitlement.query.filter_by(source_id="small").one().revoked
    assert not AccessEntitlement.query.filter_by(source_id="large").one().revoked
    assert client.enable


def test_actual_federation_hot_export_carries_both_tariff_sources(app, db):
    from panel_core.models import AccessEntitlement, Client, Inbound
    from panel_core.services.state_export import export_hot_state

    db.session.add(Inbound(tag="shared-export", protocol="vless", port=23456, stream_settings="{}"))
    owner = Client(
        id="shared-export-client",
        email="shared",
        inbound_tag="shared-export",
        telegram_id=4242,
        tariff_id=1,
        expiry_time=4102444800000,
    )
    db.session.add(owner)
    for tariff in (1, 2):
        db.session.add(
            AccessEntitlement(
                source_id=f"export:{tariff}",
                operation_id=f"export:{tariff}",
                tariff_id=tariff,
                telegram_id=4242,
                inbound_tag="shared-export",
                client_id=owner.id,
                expires_at_ms=4102444800000,
                limit_bytes=tariff * 100,
            )
        )
    db.session.commit()
    exported = next(
        row for inbound in export_hot_state()["inbounds"] for row in inbound["clients"] if row["id"] == owner.id
    )
    assert {row["tariff_id"] for row in exported["tariff_sources"]} == {1, 2}
