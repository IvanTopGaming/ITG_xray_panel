import io
import sqlite3
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask

from panel_core.extensions import db
from panel_core.models import FederationConfig, LinkedPanel, PanelStateMirror, SystemSetting


@pytest.fixture
def federation_app(tmp_path):
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'panel.db'}", TESTING=True)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.engine.dispose()


def _panel():
    row = LinkedPanel(
        name="node",
        url="https://node.invalid/secret",
        federation_token="original",
        created_at=1,
        current_instance_id="old",
        transfer_token="transfer",
        transfer_token_expires_at=int(time.time() * 1000) + 60000,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _cold():
    return {
        "outbounds": [],
        "routing_profiles": [],
        "balancers": [],
        "settings": [],
        "receipts": [],
        "notification_logs": [],
        "events": [],
        "entitlements": [],
        "account_access": [],
        "admin": None,
        "identity": {},
    }


def test_transfer_events_keep_origin_without_reusing_local_ids(federation_app):
    from panel_core.models import BotEvent
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_state

    event = BotEvent(id=7, type="payment_succeeded", telegram_id=42, payload={"amount": 100})
    db.session.add(event)
    db.session.commit()
    state = export_state()
    exported = state["cold"]["events"]
    assert len(exported) == 1
    assert exported[0]["origin_event_id"] == 7
    origin = exported[0]["source"]
    assert origin
    db.session.remove()
    assert db.session.get(BotEvent, 7).source == origin
    BotEvent.query.delete()
    db.session.add(BotEvent(id=7, source="replacement", origin_event_id=7, type="boot", payload={}))
    db.session.commit()
    apply_state(state["hot"], state["cold"], carry_admin=False)
    apply_state(state["hot"], state["cold"], carry_admin=False)
    imported = BotEvent.query.filter_by(source=origin, origin_event_id=7).one()
    assert imported.id != 7
    assert imported.payload == {"amount": 100}
    assert BotEvent.query.count() == 2


def test_event_ack_changes_cold_fingerprint_and_is_not_resurrected(federation_app):
    import datetime
    from panel_core.models import BotEvent
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_state

    event = BotEvent(type="traffic_80", telegram_id=42, payload={})
    db.session.add(event)
    db.session.commit()
    before = export_state()
    event.delivered_at = datetime.datetime.now(datetime.UTC)
    db.session.commit()
    after = export_state()
    assert after["cold"]["events"] == []
    assert after["fingerprint"] != before["fingerprint"]
    apply_state(before["hot"], before["cold"], carry_admin=False)
    assert BotEvent.query.one().delivered_at is not None


def test_transfer_preserves_entitlements_account_block_and_request_binding(federation_app):
    from panel_core.models import AccessEntitlement, AccountAccessState, ProvisionReceipt, RuntimeApplyState
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_state

    db.session.add(
        AccessEntitlement(
            source_id="pay:42",
            operation_id="pay:42",
            telegram_id=42,
            source_revision=3,
            inbound_tag="in",
            client_id="client",
            expires_at_ms=123456,
            up=123,
            down=456,
            revoked=True,
        )
    )
    db.session.add(AccountAccessState(telegram_id=42, revision=9, blocked=True))
    db.session.add(
        ProvisionReceipt(
            idempotency_key="pay:42",
            inbound_tag="in",
            telegram_id=42,
            response_json="{}",
            request_json={"period_ms": 100},
            materialized=True,
        )
    )
    db.session.commit()
    state = export_state()
    assert state["cold"]["entitlements"][0]["source_id"] == "pay:42"
    apply_state(state["hot"], state["cold"], carry_admin=False)
    entitlement = AccessEntitlement.query.one()
    assert (entitlement.up, entitlement.down, entitlement.source_revision, entitlement.revoked) == (123, 456, 3, True)
    assert AccountAccessState.query.one().blocked is True
    assert AccountAccessState.query.one().revision == 9
    assert ProvisionReceipt.query.one().request_json == {"period_ms": 100}
    assert ProvisionReceipt.query.one().materialized is False
    runtime = db.session.get(RuntimeApplyState, 1)
    assert runtime is not None and runtime.desired_revision > runtime.applied_revision


@pytest.mark.parametrize("partial", [False, True])
def test_failed_transfer_preflight_keeps_original_link(federation_app, partial):
    from panel_core.services.panel_transfer import TransferError, claim_transfer

    panel = _panel()
    if partial:
        db.session.add(
            PanelStateMirror(
                panel_id=panel.id, kind="current", taken_at=1, hot_state='{"inbounds":[]}', hot_updated_at=1
            )
        )
        db.session.commit()
    with pytest.raises(TransferError):
        claim_transfer("transfer", instance_id="new", federation_token="replacement")
    db.session.expire_all()
    assert panel.federation_token == "original"
    assert panel.current_instance_id == "old"
    assert panel.transfer_token_used is False
    assert panel.transfer_state == ""


def test_handshake_retry_returns_committed_token_and_identity(federation_app):
    from panel_core.api.federation import handshake

    db.session.add(FederationConfig(id=1, link_token="link", link_token_used=False))
    db.session.add(SystemSetting(key="node_instance_id", value="installation"))
    db.session.commit()
    request = {"link_token": "link", "master_url": "https://master.invalid/secret", "request_id": "operation"}
    with federation_app.test_request_context(json=request):
        first, first_status = handshake.__wrapped__()
    with federation_app.test_request_context(json=request):
        second, second_status = handshake.__wrapped__()
    assert (first_status, second_status) == (200, 200)
    assert first.json["federation_token"] == second.json["federation_token"]
    assert second.json["instance_id"] == "installation"


def test_cold_refresh_keeps_both_halves_of_full_response(federation_app, monkeypatch):
    from panel_core.jobs import panels
    from panel_core.services.state_mirror import load_state, read_current

    panel = _panel()
    hot = {"inbounds": [{"tag": "paid", "clients": [{"id": "paid-client"}]}]}
    cold = _cold()
    cold["receipts"] = [{"response_json": '{"client_id":"paid-client"}'}]
    monkeypatch.setattr(
        panels,
        "_fetch_cold",
        lambda *args: {
            "hot": hot,
            "cold": cold,
            "fingerprint": "after",
            "instance_id": "old",
            "timestamp": 200000000002,
        },
    )
    panels.mirror_from_snapshot(
        panel.id,
        {
            "inbounds": [],
            "instance_id": "old",
            "cold_fingerprint": "before",
            "timestamp": 200000000001,
        },
    )
    actual_hot, actual_cold = load_state(read_current(panel.id))
    assert actual_hot == hot
    assert actual_cold == cold


def test_out_of_order_mirror_write_cannot_remove_new_clients(federation_app):
    from panel_core.services.state_mirror import load_state, read_current, write_hot

    panel = _panel()
    write_hot(
        panel.id,
        {"inbounds": [{"tag": "current"}]},
        taken_at=200,
        instance_id="old",
        app_version="",
        shrink_flagged=False,
    )
    write_hot(panel.id, {"inbounds": []}, taken_at=100, instance_id="old", app_version="", shrink_flagged=False)
    assert load_state(read_current(panel.id))[0] == {"inbounds": [{"tag": "current"}]}


def test_restore_rollback_is_visible_to_orm_and_new_connections(federation_app, tmp_path, monkeypatch):
    from panel_core.api import backup

    db.session.add(SystemSetting(key="marker", value="original"))
    db.session.add(SystemSetting(key="node_instance_id", value="host-original"))
    db.session.add(FederationConfig(id=1, federation_token="current-token", master_url="https://current-master"))
    db.session.commit()
    live_path = db.engine.url.database
    candidate = tmp_path / "candidate.db"
    with sqlite3.connect(live_path) as source, sqlite3.connect(candidate) as target:
        source.backup(target)
        target.execute("UPDATE system_setting SET value='candidate' WHERE key='marker'")
        target.commit()
    monkeypatch.setattr(backup, "_db_path", lambda: live_path)
    monkeypatch.setattr(backup, "_schedule_worker_restart", lambda: None)
    monkeypatch.setattr(
        "panel_core.services.runtime_apply.generate_config_file",
        lambda: (_ for _ in ()).throw(ValueError("invalid config")),
    )
    with federation_app.test_request_context(
        "/restore",
        method="POST",
        data={
            "file": (io.BytesIO(candidate.read_bytes()), "candidate.db"),
        },
    ):
        handler = backup.restore
        while hasattr(handler, "__wrapped__"):
            handler = handler.__wrapped__
        _, status = handler()
    assert status == 500
    assert db.session.get(SystemSetting, "marker").value == "original"
    assert db.session.get(SystemSetting, "node_instance_id").value == "host-original"
    assert db.session.get(FederationConfig, 1).federation_token == "current-token"
    with sqlite3.connect(live_path) as conn:
        assert conn.execute("SELECT value FROM system_setting WHERE key='marker'").fetchone()[0] == "original"


@pytest.mark.parametrize("linked", [True, False])
def test_restore_preserves_current_host_identity_and_link(federation_app, tmp_path, monkeypatch, linked):
    from panel_core.api import backup

    db.session.add(SystemSetting(key="node_instance_id", value="this-host"))
    db.session.add(SystemSetting(key="node_transfer_claimed", value="0"))
    if linked:
        db.session.add(FederationConfig(id=1, master_url="https://current-master", federation_token="current-token"))
    db.session.commit()
    live = db.engine.url.database
    candidate = tmp_path / "foreign.db"
    with sqlite3.connect(live) as source, sqlite3.connect(candidate) as target:
        source.backup(target)
        target.execute("UPDATE system_setting SET value='foreign-host' WHERE key='node_instance_id'")
        target.execute("DELETE FROM federation_config")
        target.execute("INSERT INTO system_setting VALUES ('node_superseded_at','123')")
        target.execute(
            "INSERT INTO system_setting VALUES ('node_transfer_pending_federation_token','foreign-pending-token')"
        )
        target.execute("UPDATE system_setting SET value='1' WHERE key='node_transfer_claimed'")
        target.execute(
            "INSERT INTO federation_config (id,master_url,federation_token,link_token_used) VALUES (1,'https://foreign-master','revoked-token',1)"
        )
        target.commit()
    monkeypatch.setattr("panel_core.services.runtime_apply.generate_config_file", lambda: None)
    monkeypatch.setattr("panel_core.services.runtime_apply.restart_xray_container", lambda: None)
    backup._restore_database(str(candidate), live, f"{live}.bak")
    assert db.session.get(SystemSetting, "node_instance_id").value == "this-host"
    assert db.session.get(SystemSetting, "node_superseded_at") is None
    assert db.session.get(SystemSetting, "node_transfer_pending_federation_token") is None
    assert db.session.get(SystemSetting, "node_transfer_claimed").value == "0"
    config = db.session.get(FederationConfig, 1)
    if linked:
        assert (config.master_url, config.federation_token) == ("https://current-master", "current-token")
    else:
        assert config is None


def test_restore_migrates_legacy_candidate_before_runtime_generation(federation_app, tmp_path, monkeypatch):
    from panel_core.api import backup

    db.session.add(SystemSetting(key="node_instance_id", value="this-host"))
    db.session.commit()
    live = db.engine.url.database
    candidate = tmp_path / "legacy.db"
    with sqlite3.connect(candidate) as connection:
        connection.execute("CREATE TABLE system_setting (key VARCHAR(100) PRIMARY KEY,value TEXT)")
        connection.execute("INSERT INTO system_setting VALUES ('marker','legacy-data')")
        connection.execute("PRAGMA user_version=28")
    observed = []

    def generate():
        from panel_core.app_base import _require_schema

        _require_schema()
        observed.append(db.session.get(SystemSetting, "marker").value)

    monkeypatch.setattr("panel_core.services.runtime_apply.generate_config_file", generate)
    monkeypatch.setattr("panel_core.services.runtime_apply.restart_xray_container", lambda: None)
    backup._restore_database(str(candidate), live, f"{live}.bak")
    assert observed == ["legacy-data"]
    assert db.session.get(SystemSetting, "node_instance_id").value == "this-host"


def test_export_does_not_mix_protocol_and_credential_across_commit(federation_app):
    from sqlalchemy import event
    from panel_core.models import Client, Inbound
    from panel_core.services.state_export import export_hot_state

    db.session.add(Inbound(tag="entry", port=443, protocol="vless", stream_settings="{}"))
    db.session.add(Client(id="original-id", email="user", inbound_tag="entry", expiry_time=0))
    db.session.commit()
    db.session.remove()
    switched = []

    def switch_before_clients(connection, cursor, statement, parameters, context, executemany):
        if "FROM client" not in statement or switched:
            return
        switched.append(True)
        with sqlite3.connect(db.engine.url.database) as writer:
            writer.execute("UPDATE inbound SET protocol='trojan' WHERE tag='entry'")
            writer.execute("UPDATE client SET id='replacement-password' WHERE id='original-id'")
            writer.commit()

    event.listen(db.engine, "before_cursor_execute", switch_before_clients)
    try:
        hot = export_hot_state()
    finally:
        event.remove(db.engine, "before_cursor_execute", switch_before_clients)
    assert switched
    assert hot["inbounds"][0]["protocol"] == "vless"
    assert hot["inbounds"][0]["clients"][0]["id"] == "original-id"
    db.session.remove()
    newer = export_hot_state()
    assert newer["inbounds"][0]["protocol"] == "trojan"
    assert newer["inbounds"][0]["clients"][0]["id"] == "replacement-password"


def test_transfer_retry_keeps_original_frozen_state(federation_app):
    from panel_core.services.panel_transfer import claim_transfer
    from panel_core.services.state_mirror import read_current, write_full

    panel = _panel()
    write_full(panel.id, {"inbounds": []}, _cold(), taken_at=100, fingerprint="initial", instance_id="old")
    first = claim_transfer("transfer", instance_id="new", federation_token="replacement")
    row = read_current(panel.id)
    row.hot_state = '{"inbounds":[{"tag":"later"}]}'
    db.session.commit()
    second = claim_transfer("transfer", instance_id="new", federation_token="replacement")
    assert second == first
    assert second["hot"] == {"inbounds": []}


def test_handshake_competing_request_ids_have_single_winner(federation_app, monkeypatch):
    from panel_core.api import federation

    db.session.add(FederationConfig(id=1, link_token="link", link_token_used=False))
    db.session.add(SystemSetting(key="node_instance_id", value="installation"))
    db.session.commit()
    barrier = threading.Barrier(2)
    original = federation.secrets.token_urlsafe

    def synchronized_token(size):
        barrier.wait(timeout=5)
        return original(size)

    monkeypatch.setattr(federation.secrets, "token_urlsafe", synchronized_token)

    def invoke(request_id):
        with (
            federation_app.app_context(),
            federation_app.test_request_context(
                json={
                    "link_token": "link",
                    "request_id": request_id,
                    "master_url": "https://master.invalid/secret",
                }
            ),
        ):
            response, status = federation.handshake.__wrapped__()
            return status, response.json

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, ["first", "second"]))
    assert sorted(status for status, _ in results) == [200, 401]
    db.session.expire_all()
    winner = next(reply for status, reply in results if status == 200)
    assert db.session.get(FederationConfig, 1).federation_token == winner["federation_token"]


def test_old_poll_cannot_complete_transfer_or_replace_snapshot(federation_app, monkeypatch):
    from panel_core.jobs import panels

    panel = _panel()
    panel.current_instance_id = "replacement"
    panel.federation_token = "new-token"
    panel.transfer_state = "awaiting_dns"
    db.session.commit()
    published = []
    monkeypatch.setattr(panels, "store_panel_snapshot", lambda *args, **kwargs: published.append(args))
    old = (panel.id, "online", None, 100, panel.url, "original", 1, {"instance_id": "old", "inbounds": []}, None)
    panels._record([old])
    assert published == []
    db.session.refresh(panel)
    assert panel.transfer_state == "awaiting_dns"
    current = (
        panel.id,
        "online",
        None,
        200,
        panel.url,
        "new-token",
        2,
        {"instance_id": "replacement", "inbounds": []},
        None,
    )
    panels._record([current])
    assert len(published) == 1
    db.session.refresh(panel)
    assert panel.transfer_state == ""
    assert panel.last_poll == 200
    panels._record([old])
    assert len(published) == 1
    assert panel.last_poll == 200


def test_handshake_uses_canonical_master_path_and_relink_restores_identity(federation_app, monkeypatch):
    from panel_core.api import panels
    from panel_core.services.panel_transfer import instance_verdict

    monkeypatch.setenv("PANEL_DOMAIN", "master.invalid")
    monkeypatch.setenv("PANEL_SECRET_PATH", "secret")
    sent = []

    class Reply:
        status_code = 200

        def json(self):
            return {"federation_token": "relinked", "instance_id": "old"}

    def handshake_reply(url, **kwargs):
        sent.append(kwargs["json"])
        return Reply()

    monkeypatch.setattr(panels, "federation_post", handshake_reply)
    monkeypatch.setattr(panels, "_nudge_panel_refresh", lambda *args: None)
    panel = _panel()
    panel.current_instance_id = "replacement"
    panel.superseded_instance_id = "old"
    panel.superseded_token = "old-token"
    db.session.commit()
    with federation_app.test_request_context(json={"link_token": "fresh"}, base_url="https://other.invalid/"):
        _, status = panels.relink_panel.__wrapped__(panel.id)
    assert status == 200
    assert sent[0]["master_url"] == "https://master.invalid/secret"
    assert instance_verdict("relinked", "old")["verdict"] == "current"


def test_maintenance_excludes_existing_readers_and_new_operations(federation_app):
    from panel_core.services.maintenance import maintenance_operation

    reader_entered = threading.Event()
    release_reader = threading.Event()
    exclusive_entered = threading.Event()

    def reader():
        with federation_app.app_context(), maintenance_operation():
            reader_entered.set()
            assert release_reader.wait(5)

    def exclusive():
        with federation_app.app_context(), maintenance_operation(exclusive=True):
            exclusive_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reader_job = executor.submit(reader)
        assert reader_entered.wait(5)
        exclusive_job = executor.submit(exclusive)
        assert not exclusive_entered.wait(0.1)
        release_reader.set()
        reader_job.result(timeout=5)
        exclusive_job.result(timeout=5)
    assert exclusive_entered.is_set()
