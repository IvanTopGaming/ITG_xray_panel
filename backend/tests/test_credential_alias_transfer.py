import copy

import pytest

from panel_core.models import Client


def _state_with_alias():
    hot = {
        "inbounds": [
            {
                "tag": "in-1",
                "port": 443,
                "clients": [
                    {
                        "id": "canonical",
                        "email": "main@example",
                        "credential_aliases": [
                            {"id": "legacy", "email": "old@example", "flow": "", "wg_address": None}
                        ],
                    },
                    {"id": "other", "email": "other@example"},
                ],
            }
        ]
    }
    cold = {
        key: []
        for key in (
            "outbounds",
            "routing_profiles",
            "balancers",
            "settings",
            "receipts",
            "notification_logs",
            "events",
            "entitlements",
            "account_access",
        )
    }
    cold["identity"] = {}
    return hot, cold


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "canonical"),
        ("id", "other"),
        ("id", ""),
        ("id", 7),
        ("email", "main@example"),
        ("email", "other@example"),
        ("email", ""),
        ("client_id", "other"),
        ("inbound_tag", "wrong-inbound"),
        ("flow", []),
        ("wg_address", {}),
        ("original_data", []),
    ],
)
def test_invalid_alias_is_rejected_before_replacing_existing_state(app, db, rich_node, field, value):
    from panel_core.services.state_apply import apply_state

    hot, cold = _state_with_alias()
    hot["inbounds"][0]["clients"][0]["credential_aliases"][0][field] = value

    with pytest.raises(ValueError, match="credential|alias"):
        apply_state(hot, cold, carry_admin=False)

    assert {client.id for client in Client.query.all()} == {"uuid-1", "uuid-2"}


def test_alias_ids_are_unique_across_inbounds():
    from panel_core.services.state_mirror import validate_state

    hot, cold = _state_with_alias()
    second = copy.deepcopy(hot["inbounds"][0])
    second["tag"] = "in-2"
    second["clients"] = [second["clients"][0]]
    second["clients"][0]["id"] = "second"
    hot["inbounds"].append(second)

    with pytest.raises(ValueError, match="credential|alias"):
        validate_state(hot, cold)


def test_alias_email_can_repeat_on_another_inbound():
    from panel_core.services.state_mirror import validate_state

    hot, cold = _state_with_alias()
    second = copy.deepcopy(hot["inbounds"][0])
    second["tag"] = "in-2"
    second["clients"] = [second["clients"][0]]
    second["clients"][0]["id"] = "second"
    second["clients"][0]["credential_aliases"][0]["id"] = "second-legacy"
    hot["inbounds"].append(second)

    validate_state(hot, cold)


@pytest.mark.parametrize("aliases", [None, {}, [None], ["legacy"]])
def test_alias_payload_requires_a_list_of_objects(aliases):
    from panel_core.services.state_mirror import validate_state

    hot, cold = _state_with_alias()
    hot["inbounds"][0]["clients"][0]["credential_aliases"] = aliases

    with pytest.raises(ValueError, match="credential|alias"):
        validate_state(hot, cold)


def test_transfer_preserves_alias_credentials_and_client_generations(app, db):
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_hot_state

    hot, cold = _state_with_alias()
    canonical = hot["inbounds"][0]["clients"][0]
    canonical["access_generation"] = "access-before-transfer"
    canonical["traffic_generation"] = "traffic-before-transfer"
    canonical["credential_aliases"][0].update(flow="xtls-rprx-vision", wg_address="10.7.0.3/32")

    apply_state(hot, cold, carry_admin=False)
    transferred = next(c for c in export_hot_state()["inbounds"][0]["clients"] if c["id"] == "canonical")

    assert transferred.get("credential_aliases") == [
        {"id": "legacy", "email": "old@example", "flow": "xtls-rprx-vision", "wg_address": "10.7.0.3/32"}
    ]
    assert transferred["access_generation"] == "access-before-transfer"
    assert transferred["traffic_generation"] == "traffic-before-transfer"

    apply_state(hot, cold, carry_admin=False)
    assert len(export_hot_state()["inbounds"][0]["clients"]) == 2


def test_alias_transfer_preserves_rollback_archive_without_publishing_it(app, db):
    from panel_core.models import ClientCredential
    from panel_core.services.client_credentials import credential_aliases
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_cold_state, export_hot_state

    hot, cold = _state_with_alias()
    hot["inbounds"][0]["clients"][0]["credential_aliases"][0]["original_data"] = {
        "id": "legacy",
        "up": 128,
        "down": 256,
        "traffic_generation": "old-cycle",
    }

    apply_state(hot, cold, carry_admin=False)
    archive = db.session.get(ClientCredential, "legacy").original_data
    assert archive == {"id": "legacy", "up": 128, "down": 256, "traffic_generation": "old-cycle"}
    assert "original_data" not in credential_aliases(db.session.get(Client, "canonical"))[0]

    exported = export_hot_state()
    apply_state(exported, export_cold_state(), carry_admin=False)

    assert db.session.get(ClientCredential, "legacy").original_data == archive


def test_transfer_of_legacy_state_removes_replaced_aliases(app, db):
    from panel_core.models import ClientCredential
    from panel_core.services.state_apply import apply_state

    hot, cold = _state_with_alias()
    apply_state(hot, cold, carry_admin=False)
    hot["inbounds"][0]["clients"][0].pop("credential_aliases")

    apply_state(hot, cold, carry_admin=False)

    assert ClientCredential.query.count() == 0
