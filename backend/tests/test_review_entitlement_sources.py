import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask

from panel_core.extensions import db
from panel_core.models import Client, Inbound, ProvisionReceipt
from panel_core.services import provisioning


@pytest.fixture(autouse=True)
def runtime_counters(monkeypatch):
    from panel_core.xray.local import LocalXrayGateway

    monkeypatch.setattr(LocalXrayGateway, "read_traffic_counters", lambda self, *args: ("test-runtime", {}))


@pytest.fixture
def node_ledger(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'node.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        XRAY_CONFIG_LOCK_PATH=str(tmp_path / "runtime.lock"),
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        db.session.add(Inbound(tag="vpn", protocol="vless", port=14443, stream_settings="{}"))
        db.session.commit()
    monkeypatch.setattr(provisioning, "_require_local_xray", lambda *a: None)
    monkeypatch.setattr(provisioning, "_sync_after_provision", lambda *a: None)
    from panel_core.services import runtime_apply

    monkeypatch.setattr(runtime_apply, "generate_config_file", lambda **kw: None)
    monkeypatch.setattr(runtime_apply, "restart_xray_container", lambda: True)
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def test_two_tariffs_on_one_inbound_share_one_client(node_ledger):
    with node_ledger.app_context():
        first = provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", limit_bytes=100, period_ms=86400000, tariff_id=1, idempotency_key="pay:1"
        )
        second = provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", limit_bytes=200, period_ms=86400000, tariff_id=2, idempotency_key="pay:2"
        )
        assert first["client"]["id"] == second["client"]["id"]
        assert Client.query.count() == 1
        assert Client.query.one().limit_bytes == 200
        assert second["expires_at_ms"] == first["expires_at_ms"] + 86400000


def test_tariff_target_gate_rejects_wireguard_and_missing_inbounds(node_ledger):
    from panel_core.services.tariff_targets import validate_target

    with node_ledger.app_context():
        validate_target(None, "vpn")
        db.session.add(Inbound(tag="wg", protocol="wireguard", port=51820, stream_settings="{}"))
        db.session.commit()
        for tag in ("wg", "missing"):
            with pytest.raises(ValueError, match="subscription delivery"):
                validate_target(None, tag)


def test_account_and_revoke_counts_are_actual_transitions(node_ledger):
    from panel_core.services.entitlements import apply_account_state, revoke_source

    with node_ledger.app_context():
        provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", limit_bytes=100, period_ms=86400000, tariff_id=1, idempotency_key="pay:1"
        )
        assert apply_account_state(telegram_id=42, revision=1, blocked=True)["disabled_clients"] == 1
        assert apply_account_state(telegram_id=42, revision=2, blocked=False)["re_enabled"] == 1
        assert apply_account_state(telegram_id=42, revision=3, blocked=True)["disabled_clients"] == 1
        assert (
            revoke_source(
                telegram_id=42, tariff_id=1, inbound_tag="vpn", source_id="tariff:42:1", operation_id="revoke:1"
            )["disabled_clients"]
            == 0
        )


def test_backfill_uses_source_expiry_not_aggregate_reply(node_ledger):
    from panel_core.models import ProvisionOperation

    with node_ledger.app_context():
        end = 4102444800000
        db.session.add(
            ProvisionOperation(
                id="grant:1",
                source_id="grant:1",
                source_revision=1,
                telegram_id=42,
                tariff_id=1,
                source="admin_grant",
                kind="grant",
                status="succeeded",
                snapshot={},
                params={"expiry_ms": end},
                target_states={"1:vpn": {"reply": {"expires_at_ms": 0}}},
            )
        )
        db.session.commit()
        assert provisioning._backfill_sources(1, 42, 0)["grant:1"]["expiry_ms"] == end


def test_backfill_preserves_canonical_legacy_manual_unlimited(node_ledger):
    from panel_core.models import ProvisionOperation

    with node_ledger.app_context():
        source_id = "legacy-tariff:1:user:42"
        db.session.add(
            Client(
                id="legacy", inbound_tag="vpn", telegram_id=42, tariff_id=1, email="legacy", expiry_time=0, enable=True
            )
        )
        db.session.add(
            ProvisionOperation(
                id="rollout:previous",
                source_id=source_id,
                source_revision=0,
                telegram_id=42,
                tariff_id=1,
                source="tariff_backfill",
                kind="grant",
                status="succeeded",
                snapshot={},
                params={"expiry_ms": 4102444800000},
                target_states={"2:vpn": {"reply": {"expires_at_ms": 4102444800000}}},
            )
        )
        db.session.commit()
        assert provisioning._backfill_sources(1, 42, 0)[source_id]["expiry_ms"] == 0


def test_backfill_period_source_needs_source_specific_evidence(node_ledger):
    from panel_core.models import ProvisionOperation

    with node_ledger.app_context():
        db.session.add(
            ProvisionOperation(
                id="pay:1",
                source_id="pay:1",
                source_revision=0,
                telegram_id=42,
                tariff_id=1,
                source="yookassa",
                kind="grant",
                status="succeeded",
                snapshot={},
                params={"expiry_ms": None},
                target_states={"1:vpn": {"reply": {"expires_at_ms": 0}}},
            )
        )
        db.session.commit()
        with pytest.raises(ValueError, match="source_expiry_requires_review"):
            provisioning._backfill_sources(1, 42, 0)
        operation = db.session.get(ProvisionOperation, "pay:1")
        operation.target_states = {
            "1:vpn": {
                "reply": {
                    "expires_at_ms": 0,
                    "source_id": "pay:1",
                    "source_revision": 0,
                    "source_expires_at_ms": 4102444800000,
                }
            }
        }
        db.session.commit()
        assert provisioning._backfill_sources(1, 42, 0)["pay:1"]["expiry_ms"] == 4102444800000


def test_source_expiry_stays_own_on_replay_after_newer_purchase(node_ledger):
    with node_ledger.app_context():
        args = dict(telegram_id=42, inbound_tag="vpn", limit_bytes=100, period_ms=86400000, tariff_id=1)
        old = provisioning.provision_single_item(**args, idempotency_key="pay:old")
        newer = provisioning.provision_single_item(**args, idempotency_key="pay:new")
        replay = provisioning.provision_single_item(**args, idempotency_key="pay:old")
        assert replay["expires_at_ms"] == newer["expires_at_ms"]
        assert replay["source_expires_at_ms"] == old["expires_at_ms"]
        assert replay["source_id"] == "pay:old"
        assert replay["source_revision"] == 0


def test_tariff_revoke_replay_does_not_revoke_a_later_purchase(node_ledger):
    from panel_core.services.entitlements import revoke_source

    with node_ledger.app_context():
        args = dict(telegram_id=42, inbound_tag="vpn", limit_bytes=100, period_ms=86400000, tariff_id=1)
        provisioning.provision_single_item(**args, idempotency_key="pay:old")
        revoke_source(
            telegram_id=42, inbound_tag="vpn", tariff_id=1, source_id="tariff:42:1", operation_id="revoke:one"
        )
        new = provisioning.provision_single_item(**args, idempotency_key="pay:new")
        revoke_source(
            telegram_id=42, inbound_tag="vpn", tariff_id=1, source_id="tariff:42:1", operation_id="revoke:one"
        )
        client = Client.query.one()
        assert client.enable
        assert client.expiry_time == new["expires_at_ms"]


def test_reset_cycle_receipt_preserves_new_usage_and_other_source(node_ledger):
    from panel_core.services.entitlements import reset_source_cycle

    with node_ledger.app_context():
        args = dict(telegram_id=42, inbound_tag="vpn", limit_bytes=100, period_ms=86400000, tariff_id=1)
        provisioning.provision_single_item(**args, idempotency_key="grant:1", source_id="grant:1", source_revision=1)
        cycle = dict(telegram_id=42, inbound_tag="vpn", source_id="grant:1", source_revision=1, operation_id="cycle:1")
        reset_source_cycle(**cycle)
        client = Client.query.one()
        client.up = 25
        db.session.commit()
        reset_source_cycle(**cycle)
        assert client.up == 25
        provisioning.provision_single_item(**args, idempotency_key="pay:new")
        client.up = 50
        db.session.commit()
        reset_source_cycle(**{**cycle, "operation_id": "cycle:2"})
        assert client.up == 50
        assert client.active_entitlement_source == "pay:new"


def test_late_grant_cannot_cross_a_tariff_revocation_tombstone(node_ledger):
    from panel_core.services.entitlements import revoke_source

    with node_ledger.app_context():
        revoke_source(
            telegram_id=42,
            inbound_tag="vpn",
            tariff_id=1,
            source_id="tariff:42:1",
            operation_id="revoke:one",
            revoked_sources={"pay:old": 1},
        )
        with pytest.raises(ValueError, match="superseded"):
            provisioning.provision_single_item(
                telegram_id=42,
                inbound_tag="vpn",
                limit_bytes=100,
                period_ms=86400000,
                tariff_id=1,
                idempotency_key="pay:old",
            )
        assert Client.query.count() == 0


def test_obsolete_operation_is_terminal_and_does_not_deliver(node_ledger, monkeypatch):
    from panel_core.models import ProvisionOperation
    from panel_core.services import provisioning_operations as ops

    with node_ledger.app_context():
        args = dict(
            telegram_id=42,
            tariff_id=1,
            source="admin_grant",
            source_id="grant:1",
            snapshot={"items": [{"panel_id": 2, "inbound_tag": "vpn", "traffic_gb": 1}], "period_days": 30},
            params={},
        )
        first = ops.queue_operation(**args, operation_id="grant:1:1", source_revision=1)
        ops.queue_operation(**args, operation_id="grant:1:2", source_revision=2)
        calls = []
        monkeypatch.setattr(ops, "proxy_provision", lambda *a: calls.append(a))
        assert ops.run_operation(first)["status"] == "superseded"
        assert db.session.get(ProvisionOperation, first.id).status == "superseded"
        assert not calls


def test_stale_operation_owner_cannot_overwrite_takeover_progress(node_ledger, monkeypatch):
    from panel_core.models import ProvisionOperation
    from panel_core.services import provisioning_operations as ops
    from sqlalchemy import update

    with node_ledger.app_context():
        operation = ops.queue_operation(
            telegram_id=42,
            tariff_id=1,
            source="admin_grant",
            source_id="grant:1",
            source_revision=1,
            operation_id="grant:1:1",
            snapshot={"items": [{"panel_id": 2, "inbound_tag": "vpn", "traffic_gb": 1}], "period_days": 30},
            params={},
        )

        def takeover(*args):
            db.session.execute(
                update(ProvisionOperation)
                .where(ProvisionOperation.id == operation.id)
                .values(
                    processing_owner="new-owner",
                    processing_version=ProvisionOperation.processing_version + 1,
                    target_states={"takeover": {"status": "pending"}},
                )
            )
            db.session.commit()
            return {"expires_at_ms": 123, "client": {}}

        monkeypatch.setattr(ops, "proxy_provision", takeover)
        with pytest.raises(RuntimeError, match="lease_lost"):
            ops.run_operation(operation)
        db.session.refresh(operation)
        assert operation.processing_owner == "new-owner"
        assert operation.target_states == {"takeover": {"status": "pending"}}


def test_receipt_cannot_replay_different_account_or_terms(node_ledger):
    with node_ledger.app_context():
        provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", limit_bytes=100, period_ms=86400000, tariff_id=1, idempotency_key="pay:1"
        )
        with pytest.raises(ValueError):
            provisioning.provision_single_item(
                telegram_id=43,
                inbound_tag="vpn",
                limit_bytes=999,
                period_ms=172800000,
                tariff_id=1,
                idempotency_key="pay:1",
            )


def test_missing_receipt_client_is_not_reported_materialized(node_ledger):
    with node_ledger.app_context():
        reply = provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", limit_bytes=100, period_ms=86400000, tariff_id=1, idempotency_key="pay:1"
        )
        Client.query.delete()
        receipt = ProvisionReceipt.query.one()
        receipt.materialized = False
        db.session.commit()
        with pytest.raises((ValueError, RuntimeError)):
            provisioning.provision_single_item(
                telegram_id=42,
                inbound_tag="vpn",
                limit_bytes=100,
                period_ms=86400000,
                tariff_id=1,
                idempotency_key="pay:1",
            )
        assert Client.query.count() == 0
        assert receipt.materialized is False
        assert json.loads(receipt.response_json)["client"]["id"] == reply["client"]["id"]


def test_concurrent_first_purchases_share_one_client_across_tariffs(node_ledger):
    def grant(index):
        with node_ledger.app_context():
            return provisioning.provision_single_item(
                telegram_id=42,
                inbound_tag="vpn",
                limit_bytes=100,
                period_ms=86400000,
                tariff_id=index,
                idempotency_key=f"pay:{index}",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(grant, [1, 2]))
    with node_ledger.app_context():
        assert Client.query.count() == 1
        dates = sorted(result["expires_at_ms"] for result in results)
        assert dates[1] - dates[0] == 86400000


def test_refunding_older_purchase_preserves_new_purchase_endpoint(node_ledger):
    from panel_core.services.entitlements import revoke_source

    with node_ledger.app_context():
        first = provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", limit_bytes=100, period_ms=86400000, tariff_id=1, idempotency_key="pay:1"
        )
        second = provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", limit_bytes=200, period_ms=86400000, tariff_id=1, idempotency_key="pay:2"
        )
        revoke_source(telegram_id=42, inbound_tag="vpn", source_id="pay:1", tariff_id=1)
        client = db.session.get(Client, first["client"]["id"])
        assert client.enable is True
        assert client.expiry_time == second["expires_at_ms"]
        assert client.limit_bytes == 200
        with pytest.raises(ValueError):
            provisioning.provision_single_item(
                telegram_id=42,
                inbound_tag="vpn",
                limit_bytes=100,
                period_ms=86400000,
                tariff_id=1,
                idempotency_key="pay:1",
            )


def test_manual_extension_survives_refund_of_all_purchases(node_ledger):
    from panel_core.services.entitlements import capture_manual_entitlement, revoke_source

    with node_ledger.app_context():
        result = provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", limit_bytes=100, period_ms=86400000, tariff_id=1, idempotency_key="pay:1"
        )
        client = db.session.get(Client, result["client"]["id"])
        manual_end = result["expires_at_ms"] + 86400000
        client.expiry_time = manual_end
        capture_manual_entitlement(client)
        db.session.commit()
        revoke_source(telegram_id=42, inbound_tag="vpn", source_id="pay:1", tariff_id=1)
        assert client.enable is True
        assert client.expiry_time == manual_end


def test_account_fence_rejects_old_grant_and_preserves_manual_disable(node_ledger):
    from panel_core.services.entitlements import apply_account_state, capture_manual_entitlement

    with node_ledger.app_context():
        result = provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", limit_bytes=100, period_ms=86400000, tariff_id=1, idempotency_key="pay:1"
        )
        client = db.session.get(Client, result["client"]["id"])
        client.enable = False
        capture_manual_entitlement(client)
        db.session.commit()
        apply_account_state(telegram_id=42, revision=1, blocked=True)
        with pytest.raises(ValueError):
            provisioning.provision_single_item(
                telegram_id=42,
                inbound_tag="vpn",
                limit_bytes=100,
                period_ms=86400000,
                tariff_id=1,
                idempotency_key="pay:2",
                account_revision=0,
            )
        apply_account_state(telegram_id=42, revision=2, blocked=False)
        assert client.enable is False


def test_failed_runtime_apply_retries_committed_entitlement_without_second_period(node_ledger, monkeypatch):
    from panel_core.services import runtime_apply

    with node_ledger.app_context():
        monkeypatch.setattr("panel_core.services.entitlements._api_add_user_grpc", lambda *args: False)
        monkeypatch.setattr(
            runtime_apply, "restart_xray_container", lambda: (_ for _ in ()).throw(RuntimeError("offline"))
        )
        with pytest.raises(runtime_apply.RuntimeApplyError):
            provisioning.provision_single_item(
                telegram_id=42,
                inbound_tag="vpn",
                limit_bytes=100,
                period_ms=86400000,
                tariff_id=1,
                idempotency_key="pay:1",
            )
        end = Client.query.one().expiry_time
        assert ProvisionReceipt.query.one().materialized is False
        monkeypatch.setattr(runtime_apply, "restart_xray_container", lambda: True)
        result = provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", limit_bytes=100, period_ms=86400000, tariff_id=1, idempotency_key="pay:1"
        )
        assert result["expires_at_ms"] == end
        assert ProvisionReceipt.query.one().materialized is True


def test_two_node_paid_operation_recovers_after_crash_without_duplicate_extension(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from panel_core.models import LinkedPanel, ProvisionOperation
    from panel_core.services import runtime_apply, provisioning_operations
    from panel_core.services.entitlements import provision as provision_on_node
    from panel_core.services.panel_proxy import FederationClient

    apps = {}
    for name in ("master", "node-a", "node-b"):
        app = Flask(name)
        app.config.update(
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / (name + '.db')}",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            XRAY_CONFIG_LOCK_PATH=str(tmp_path / (name + ".lock")),
        )
        db.init_app(app)
        apps[name] = app
        with app.app_context():
            db.create_all()
            if name != "master":
                db.session.add(Inbound(tag="vpn", protocol="vless", port=14443, stream_settings="{}"))
            else:
                db.session.add_all(
                    [
                        LinkedPanel(
                            id=1, name="a", url="https://node-a", federation_token="a", enable=True, created_at=1
                        ),
                        LinkedPanel(
                            id=2, name="b", url="https://node-b", federation_token="b", enable=True, created_at=1
                        ),
                    ]
                )
            db.session.commit()
    monkeypatch.setattr(runtime_apply, "generate_config_file", lambda **kw: None)
    monkeypatch.setattr(runtime_apply, "restart_xray_container", lambda: True)
    monkeypatch.setattr(provisioning_operations, "has_local_xray", lambda: False)
    interrupted = [False]

    class ProcessStopped(BaseException):
        pass

    def transport(self, telegram_id, inbound_tag, params):
        name = "node-a" if self.base_url.endswith("node-a") else "node-b"
        if name == "node-b" and not interrupted[0]:
            interrupted[0] = True
            raise ProcessStopped()
        with apps[name].app_context():
            return provision_on_node(telegram_id=telegram_id, inbound_tag=inbound_tag, **params)

    monkeypatch.setattr(FederationClient, "provision", transport)
    tariff = SimpleNamespace(
        id=5,
        name="two nodes",
        period_days=30,
        items=[
            SimpleNamespace(panel_id=1, inbound_tag="vpn", traffic_gb=1),
            SimpleNamespace(panel_id=2, inbound_tag="vpn", traffic_gb=1),
        ],
    )
    with apps["master"].app_context():
        with pytest.raises(ProcessStopped):
            provisioning.apply_tariff_for_user(42, tariff, source="yookassa", operation_id="pay:500")
        assert ProvisionOperation.query.one().status == "pending"
        db.session.remove()
    with apps["node-a"].app_context():
        original_end = Client.query.one().expiry_time
    with apps["master"].app_context():
        result = provisioning.apply_tariff_for_user(42, tariff, source="yookassa", operation_id="pay:500")
        assert result["status"] == "succeeded"
    with apps["node-a"].app_context():
        assert Client.query.one().expiry_time == original_end
        assert ProvisionReceipt.query.count() == 1
    with apps["node-b"].app_context():
        assert Client.query.count() == 1
        assert Client.query.one().limit_bytes == 1024**3


@pytest.mark.parametrize(
    "old_limit,new_limit,expected", [(100, 10, 100), (10, 100, 100), (0, 10, 0), (10, 0, 0), (100, 100, 100)]
)
def test_shared_purchase_preserves_key_extends_and_resets_selected_quota(
    node_ledger, monkeypatch, old_limit, new_limit, expected
):
    from panel_core.models import AccessEntitlement
    from panel_core.services import entitlements

    monkeypatch.setattr(entitlements, "_now_ms", lambda: 100000)
    with node_ledger.app_context():
        first = provisioning.provision_single_item(
            telegram_id=42,
            inbound_tag="vpn",
            tariff_id=1,
            limit_bytes=old_limit,
            period_ms=10000,
            idempotency_key="trial:first",
        )
        client = Client.query.one()
        client.up, client.down = 3, 4
        db.session.commit()
        second = provisioning.provision_single_item(
            telegram_id=42,
            inbound_tag="vpn",
            tariff_id=2,
            limit_bytes=new_limit,
            period_ms=30000,
            idempotency_key="pay:second",
        )
        assert Client.query.count() == 1
        assert second["client"]["id"] == first["client"]["id"]
        assert client.expiry_time == 140000
        assert client.limit_bytes == expected
        assert (client.up, client.down) == (0, 0)
        assert AccessEntitlement.query.filter_by(source_id="trial:first").one().expires_at_ms == 110000
        client.up = 2
        db.session.commit()
        provisioning.provision_single_item(
            telegram_id=42,
            inbound_tag="vpn",
            tariff_id=2,
            limit_bytes=new_limit,
            period_ms=30000,
            idempotency_key="pay:second",
        )
        assert client.up == 2
        assert client.expiry_time == 140000


def test_shared_quota_expiry_starts_fresh_lower_cycle_only_once(node_ledger, monkeypatch):
    from panel_core.models import AccessEntitlement
    from panel_core.services import entitlements

    monkeypatch.setattr(entitlements, "_now_ms", lambda: 100000)
    with node_ledger.app_context():
        provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", tariff_id=1, limit_bytes=10, period_ms=10000, idempotency_key="pay:low"
        )
        client = Client.query.one()
        client.up = 6
        db.session.commit()
        provisioning.provision_single_item(
            telegram_id=42,
            inbound_tag="vpn",
            tariff_id=2,
            limit_bytes=100,
            expiry_ms=105000,
            operation_id="grant:high",
            source_id="grant:high",
        )
        assert client.limit_bytes == 100
        client.up = 30
        db.session.commit()
        entitlements.refresh_client_entitlements(client, now_ms=105001)
        assert client.limit_bytes == 10
        assert client.expiry_time == 110000
        assert client.up == client.down == 0
        assert AccessEntitlement.query.filter_by(source_id="grant:high").one().up == 30
        generation = client.traffic_generation
        assert generation
        client.down = 4
        db.session.commit()
        entitlements.refresh_client_entitlements(client, now_ms=105002)
        assert client.down == 4
        assert client.traffic_generation == generation


def test_revoke_one_tariff_preserves_other_source_and_usage(node_ledger, monkeypatch):
    from panel_core.models import AccessEntitlement
    from panel_core.services import entitlements

    monkeypatch.setattr(entitlements, "_now_ms", lambda: 100000)
    with node_ledger.app_context():
        provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", tariff_id=1, limit_bytes=100, period_ms=10000, idempotency_key="pay:one"
        )
        provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", tariff_id=2, limit_bytes=10, period_ms=10000, idempotency_key="pay:two"
        )
        client = Client.query.one()
        client.up = 35
        db.session.commit()
        result = entitlements.revoke_source(
            telegram_id=42, inbound_tag="vpn", tariff_id=2, source_id="tariff:42:2", operation_id="revoke:two"
        )
        assert result["disabled_clients"] == 0
        assert client.enable
        assert client.up == 35
        assert client.limit_bytes == 100
        assert client.expiry_time == 110000
        assert not AccessEntitlement.query.filter_by(source_id="pay:one").one().revoked
        assert AccessEntitlement.query.filter_by(source_id="pay:two").one().revoked


def test_refund_selected_source_restores_prior_usage_without_free_reset(node_ledger, monkeypatch):
    from panel_core.services import entitlements

    monkeypatch.setattr(entitlements, "_now_ms", lambda: 100000)
    with node_ledger.app_context():
        provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", tariff_id=1, limit_bytes=10, period_ms=10000, idempotency_key="pay:one"
        )
        client = Client.query.one()
        client.up = 6
        db.session.commit()
        provisioning.provision_single_item(
            telegram_id=42,
            inbound_tag="vpn",
            tariff_id=2,
            limit_bytes=100,
            expiry_ms=105000,
            operation_id="grant:high",
            source_id="grant:high",
        )
        entitlements.revoke_source(telegram_id=42, inbound_tag="vpn", tariff_id=2, source_id="grant:high")
        assert client.up == 6
        assert client.limit_bytes == 10
        assert client.enable


def test_exhausted_legacy_trial_keeps_remaining_time_on_paid_purchase(node_ledger, monkeypatch):
    from panel_core.services import entitlements

    monkeypatch.setattr(entitlements, "_now_ms", lambda: 100000)
    with node_ledger.app_context():
        db.session.add(
            Client(
                id="old-trial",
                email="trial",
                inbound_tag="vpn",
                telegram_id=42,
                tariff_id=1,
                expiry_time=110000,
                limit_bytes=100,
                up=100,
                down=0,
                enable=False,
                disable_reason="quota",
            )
        )
        db.session.commit()
        result = provisioning.provision_single_item(
            telegram_id=42, inbound_tag="vpn", tariff_id=2, limit_bytes=10, period_ms=30000, idempotency_key="pay:new"
        )
        assert result["client"]["id"] == "old-trial"
        assert result["expires_at_ms"] == 140000
        assert result["client"]["limit_bytes"] == 100
        assert result["client"]["enable"]


def test_absolute_source_update_resets_its_own_usage(node_ledger, monkeypatch):
    from panel_core.services import entitlements

    monkeypatch.setattr(entitlements, "_now_ms", lambda: 100000)
    with node_ledger.app_context():
        arguments = dict(
            telegram_id=42, inbound_tag="vpn", tariff_id=1, limit_bytes=100, expiry_ms=110000, source_id="grant:one"
        )
        provisioning.provision_single_item(**arguments, operation_id="grant:one:1", source_revision=1)
        client = Client.query.one()
        client.up = 30
        db.session.commit()
        provisioning.provision_single_item(**arguments, operation_id="grant:one:2", source_revision=2)
        assert client.up == client.down == 0


def test_delayed_tariff_revocation_leaves_newer_purchase(node_ledger, monkeypatch):
    from panel_core.services import entitlements
    from panel_core.models import AccessEntitlement

    monkeypatch.setattr(entitlements, "_now_ms", lambda: 100000)
    with node_ledger.app_context():
        arguments = dict(telegram_id=42, inbound_tag="vpn", tariff_id=1, limit_bytes=100, period_ms=10000)
        provisioning.provision_single_item(**arguments, idempotency_key="pay:old")
        provisioning.provision_single_item(**arguments, idempotency_key="pay:new")
        entitlements.revoke_source(
            telegram_id=42,
            inbound_tag="vpn",
            tariff_id=1,
            source_id="tariff:42:1",
            operation_id="revoke:old",
            revoked_sources={"pay:old": 1},
        )
        assert Client.query.one().enable
        assert not AccessEntitlement.query.filter_by(source_id="pay:new").one().revoked


def test_manual_quota_survives_account_unblock_until_next_purchase(node_ledger, monkeypatch):
    from panel_core.services import entitlements

    monkeypatch.setattr(entitlements, "_now_ms", lambda: 100000)
    with node_ledger.app_context():
        args = dict(telegram_id=42, inbound_tag="vpn", tariff_id=1, period_ms=10000)
        provisioning.provision_single_item(**args, limit_bytes=100, idempotency_key="pay:one")
        client = Client.query.one()
        client.limit_bytes = 10
        entitlements.capture_manual_entitlement(client)
        db.session.commit()
        entitlements.apply_account_state(telegram_id=42, revision=1, blocked=True)
        entitlements.apply_account_state(telegram_id=42, revision=2, blocked=False)
        assert client.limit_bytes == 10
        provisioning.provision_single_item(**args, limit_bytes=100, idempotency_key="pay:two", account_revision=2)
        assert client.limit_bytes == 100
