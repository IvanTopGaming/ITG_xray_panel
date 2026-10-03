import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from panel_core.db_migration import CURRENT_DB_VERSION
from panel_core.extensions import db
from panel_core.models import Outbound, RuntimeApplyState, TrafficCounterBaseline
from panel_core.xray import engine, grpc_client


@pytest.fixture
def existing_runtime(app, monkeypatch, tmp_path):
    db.session.execute(text(f"PRAGMA user_version = {CURRENT_DB_VERSION}"))
    db.session.add_all(
        [
            Outbound(tag="direct", protocol="freedom"),
            Outbound(tag="block", protocol="blackhole"),
            RuntimeApplyState(id=1, desired_revision=7, applied_revision=7, last_error=""),
        ]
    )
    db.session.commit()
    engine.generate_config_file(validate=False)
    published = Path(engine.CONFIG_PATH).read_bytes()
    validator = tmp_path / "xray"
    validator.write_text("#!/bin/sh\nexit 0\n")
    validator.chmod(0o700)
    monkeypatch.setattr(engine, "XRAY_BIN", str(validator))
    attrs = {
        "Id": "existing-container",
        "Image": "existing-image",
        "RestartCount": 0,
        "State": {
            "Running": True,
            "Paused": False,
            "Restarting": False,
            "Pid": 123,
            "StartedAt": "2026-10-03T00:00:00.000000000Z",
        },
    }
    docker_client = Mock()
    docker_client.api.inspect_container.return_value = attrs
    monkeypatch.setattr(engine.docker, "from_env", lambda **kwargs: docker_client)
    monkeypatch.setattr(grpc_client, "_runtime_epoch", lambda: attrs["State"]["StartedAt"])
    stub = Mock()
    stub.QueryStats.return_value = SimpleNamespace(stat=[])
    monkeypatch.setattr(grpc_client.stats_command_pb2_grpc, "StatsServiceStub", lambda channel: stub)
    monkeypatch.setattr(grpc_client.stats_command_pb2, "QueryStatsRequest", lambda **kwargs: SimpleNamespace(**kwargs))
    return SimpleNamespace(attrs=attrs, docker=docker_client, stub=stub, published=published)


def preserve():
    from panel_core.xray.startup import preserve_existing_runtime

    return preserve_existing_runtime()


def assert_untouched(existing_runtime):
    assert Path(engine.CONFIG_PATH).read_bytes() == existing_runtime.published
    state = db.session.get(RuntimeApplyState, 1, populate_existing=True)
    assert (state.desired_revision, state.applied_revision) == (7, 7)
    existing_runtime.docker.api.restart.assert_not_called()


def test_preserve_keeps_config_revision_and_nonreset_counters(existing_runtime):
    baseline = TrafficCounterBaseline(
        entity_type="user",
        entity_id="existing-user",
        inbound_tag="existing-inbound",
        runtime_epoch=existing_runtime.attrs["State"]["StartedAt"],
        runtime_identity="existing-identity",
        traffic_generation="existing-generation",
        up=123,
        down=456,
    )
    db.session.add(baseline)
    db.session.commit()
    preserve()
    assert_untouched(existing_runtime)
    db.session.refresh(baseline)
    assert (baseline.up, baseline.down, baseline.traffic_generation) == (123, 456, "existing-generation")
    assert existing_runtime.stub.QueryStats.call_args.args[0].reset is False
    assert not Path(engine.CANDIDATE_PATH).exists()


@pytest.mark.parametrize("field,value", [("desired_revision", 8), ("last_error", "previous failure")])
def test_preserve_refuses_unclean_revision(existing_runtime, field, value):
    setattr(db.session.get(RuntimeApplyState, 1), field, value)
    db.session.commit()
    with pytest.raises(RuntimeError, match="runtime state"):
        preserve()
    assert Path(engine.CONFIG_PATH).read_bytes() == existing_runtime.published
    existing_runtime.docker.api.restart.assert_not_called()


def test_preserve_refuses_missing_revision(existing_runtime):
    db.session.delete(db.session.get(RuntimeApplyState, 1))
    db.session.commit()
    with pytest.raises(RuntimeError, match="runtime state"):
        preserve()
    assert db.session.get(RuntimeApplyState, 1) is None


def test_preserve_refuses_missing_validator(existing_runtime, monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "XRAY_BIN", str(tmp_path / "absent"))
    with pytest.raises(RuntimeError, match="validation.*unavailable"):
        preserve()
    assert_untouched(existing_runtime)


def test_preserve_refuses_changed_config(existing_runtime):
    db.session.get(Outbound, 1).settings = '{"domainStrategy":"UseIPv4"}'
    db.session.commit()
    with pytest.raises(RuntimeError, match="configuration differs"):
        preserve()
    assert_untouched(existing_runtime)


def test_preserve_keeps_array_order_significant(existing_runtime):
    config = json.loads(existing_runtime.published)
    config["outbounds"].reverse()
    Path(engine.CONFIG_PATH).write_text(json.dumps(config))
    with pytest.raises(RuntimeError, match="configuration differs"):
        preserve()


def test_preserve_ignores_object_key_order_and_whitespace(existing_runtime):
    config = json.loads(existing_runtime.published)
    published = json.dumps(config, sort_keys=True).encode()
    Path(engine.CONFIG_PATH).write_bytes(published)
    preserve()
    assert Path(engine.CONFIG_PATH).read_bytes() == published


def test_preserve_does_not_equate_json_boolean_and_number(existing_runtime):
    config = json.loads(existing_runtime.published)
    config["policy"]["levels"]["0"]["statsUserUplink"] = 1
    Path(engine.CONFIG_PATH).write_text(json.dumps(config))
    with pytest.raises(RuntimeError, match="configuration differs"):
        preserve()


@pytest.mark.parametrize("field,value", [("Running", False), ("Paused", True), ("Restarting", True), ("Pid", 0)])
def test_preserve_refuses_unavailable_runtime(existing_runtime, field, value):
    existing_runtime.attrs["State"][field] = value
    with pytest.raises(RuntimeError, match="identity is unavailable"):
        preserve()
    assert_untouched(existing_runtime)


def test_preserve_refuses_replaced_container(existing_runtime):
    replaced = {**existing_runtime.attrs, "Id": "replacement-container"}
    existing_runtime.docker.api.inspect_container.side_effect = [existing_runtime.attrs, replaced]
    with pytest.raises(RuntimeError, match="identity changed"):
        preserve()
    assert_untouched(existing_runtime)


def test_preserve_refuses_invalid_config_without_publication(existing_runtime):
    Path(engine.XRAY_BIN).write_text("#!/bin/sh\necho 'config rejected' >&2\nexit 1\n")
    with pytest.raises(ValueError, match="Xray rejected"):
        preserve()
    assert_untouched(existing_runtime)
    assert not Path(engine.CANDIDATE_PATH).exists()


def test_preserve_refuses_concurrent_config_replacement(existing_runtime):
    def query(*args, **kwargs):
        Path(engine.CONFIG_PATH).write_text("{}")
        return SimpleNamespace(stat=[])

    existing_runtime.stub.QueryStats.side_effect = query
    with pytest.raises(RuntimeError, match="configuration changed"):
        preserve()
    assert Path(engine.CONFIG_PATH).read_text() == "{}"
    existing_runtime.docker.api.restart.assert_not_called()


@pytest.mark.parametrize("field,value", [("Pid", 456), ("StartedAt", "2026-10-03T01:00:00Z")])
def test_preserve_refuses_process_change(existing_runtime, field, value):
    changed = {**existing_runtime.attrs, "State": {**existing_runtime.attrs["State"], field: value}}
    existing_runtime.docker.api.inspect_container.side_effect = [existing_runtime.attrs, changed]
    with pytest.raises(RuntimeError, match="identity changed"):
        preserve()
    assert_untouched(existing_runtime)


def test_preserve_refuses_grpc_failure(existing_runtime):
    existing_runtime.stub.QueryStats.side_effect = RuntimeError("grpc unavailable")
    with pytest.raises(RuntimeError, match="grpc unavailable"):
        preserve()
    assert_untouched(existing_runtime)


def test_preserve_refuses_old_schema_without_migrating(existing_runtime):
    db.session.execute(text(f"PRAGMA user_version = {CURRENT_DB_VERSION - 1}"))
    with pytest.raises(RuntimeError, match="schema version"):
        preserve()
    assert db.session.execute(text("PRAGMA user_version")).scalar() == CURRENT_DB_VERSION - 1
    assert_untouched(existing_runtime)


@pytest.fixture
def worker_factory(app, monkeypatch):
    from panel_core.roles import worker

    monkeypatch.setattr(worker, "build_base_app", lambda role: app)
    monkeypatch.setattr(worker, "ensure_scheduler_job", Mock())
    monkeypatch.setattr(worker, "start_scheduler", Mock())
    monkeypatch.setattr(worker, "migrate_schema", Mock(side_effect=AssertionError("migration attempted")))
    monkeypatch.setattr(worker, "bootstrap_defaults", Mock(side_effect=AssertionError("bootstrap overwrote config")))
    monkeypatch.setattr(worker, "mark_runtime_dirty", Mock(side_effect=AssertionError("marked dirty")))
    monkeypatch.setattr(worker, "synchronize_runtime", Mock(side_effect=AssertionError("runtime restarted")))
    return worker


def test_worker_preserve_skips_bootstrap_publication(existing_runtime, worker_factory, monkeypatch):
    monkeypatch.setenv("XRAY_STARTUP_MODE", "preserve")
    worker_factory.create_app()
    worker_factory.start_scheduler.assert_called_once()
    assert_untouched(existing_runtime)


def test_worker_preserve_failure_does_not_start_scheduler(existing_runtime, worker_factory, monkeypatch):
    monkeypatch.setenv("XRAY_STARTUP_MODE", "preserve")
    db.session.get(RuntimeApplyState, 1).desired_revision = 8
    db.session.commit()
    with pytest.raises(RuntimeError, match="runtime state"):
        worker_factory.create_app()
    worker_factory.start_scheduler.assert_not_called()


def test_worker_invalid_startup_mode_fails_before_startup(worker_factory, monkeypatch):
    monkeypatch.setenv("XRAY_STARTUP_MODE", "presreve")
    with pytest.raises(ValueError, match="XRAY_STARTUP_MODE"):
        worker_factory.create_app()
    worker_factory.start_scheduler.assert_not_called()


@pytest.mark.parametrize("mode", [None, "synchronize"])
def test_normal_worker_startup_still_migrates_and_synchronizes(worker_factory, monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("XRAY_STARTUP_MODE", raising=False)
    else:
        monkeypatch.setenv("XRAY_STARTUP_MODE", mode)
    operations = []
    for name in (
        "migrate_schema",
        "bootstrap_defaults",
        "mark_runtime_dirty",
        "synchronize_runtime",
        "start_scheduler",
    ):
        monkeypatch.setattr(worker_factory, name, lambda *args, _name=name, **kwargs: operations.append(_name))
    worker_factory.create_app()
    assert operations == [
        "migrate_schema",
        "bootstrap_defaults",
        "mark_runtime_dirty",
        "synchronize_runtime",
        "start_scheduler",
    ]
