import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from panel_core.extensions import db
from panel_core.models import Client, DomainStat, Inbound, NotificationLog
from panel_core.services import traffic_store
from panel_core.services.runtime_identity import build_runtime_email


@pytest.fixture
def traffic(monkeypatch, tmp_path):
    stubs = os.environ.get("XRAY_PROTO_PATH", "/tmp/itg-xray-audit.s88BH2/stubs")
    if not Path(stubs).is_dir():
        pytest.skip("Set XRAY_PROTO_PATH to generated Xray protobuf modules")
    monkeypatch.syspath_prepend(stubs)
    from panel_core.services import runtime_apply, stats

    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'traffic.sqlite'}",
        XRAY_CONFIG_LOCK_PATH=str(tmp_path / "runtime.lock"),
    )
    db.init_app(app)

    class Gateway:
        def __init__(self):
            self.values = {}
            self.epoch = "epoch-one"
            self.active = {"person"}

        def has_local_xray(self):
            return True

        def read_traffic_counters(self, pattern=""):
            return self.epoch, {key: value for key, value in self.values.items() if pattern in key}

        def reset_user_counters(self, *args):
            raise AssertionError("destructive reads are forbidden")

        def reset_inbound_counters(self, *args):
            raise AssertionError("destructive reads are forbidden")

        def remove_user(self, tag, email):
            self.active.discard(email)
            return True

        def add_user(self, tag, client):
            self.active.add(client.email)
            return True

        def restart(self):
            self.active = {client.email for client in Client.query.filter_by(enable=True).all()}

    gateway = Gateway()

    def old_query(request, **kwargs):
        value = gateway.values.get(request.pattern, 0)
        if request.reset:
            gateway.values[request.pattern] = 0
        return SimpleNamespace(stat=[SimpleNamespace(name=request.pattern, value=value)])

    monkeypatch.setattr(stats, "get_channel", lambda: None)
    monkeypatch.setattr(
        stats.stats_command_pb2_grpc, "StatsServiceStub", lambda channel: SimpleNamespace(QueryStats=old_query)
    )
    monkeypatch.setattr(traffic_store, "get_xray_gateway", lambda: gateway)
    monkeypatch.setattr("panel_core.xray.gateway.get_xray_gateway", lambda: gateway)
    monkeypatch.setattr(runtime_apply, "generate_config_file", lambda **kwargs: None)
    monkeypatch.setattr(runtime_apply, "restart_xray_container", gateway.restart)
    monkeypatch.setattr(stats, "generate_config_file", lambda: None, raising=False)
    monkeypatch.setattr(stats, "restart_xray_container", gateway.restart, raising=False)
    monkeypatch.setattr("panel_core.services.entitlements._api_remove_user_grpc", gateway.remove_user)
    monkeypatch.setattr("panel_core.services.entitlements._api_add_user_grpc", gateway.add_user)
    monkeypatch.setattr(stats, "ACCESS_LOG_PATH", str(tmp_path / "access.log"))
    monkeypatch.setattr(stats, "ACCESS_LOG_OFFSET_PATH", str(tmp_path / "access.offset"))
    monkeypatch.setattr(stats, "ERROR_LOG_PATH", str(tmp_path / "error.log"))
    monkeypatch.setattr(stats, "REALITY_OFFSET_PATH", str(tmp_path / "reality.offset"))
    with app.app_context():
        db.create_all()
        db.session.add(Inbound(tag="ib", protocol="vless", port=12345, stream_settings="{}"))
        db.session.add(Client(id=str(uuid.uuid4()), email="person", inbound_tag="ib", up=0, down=0, enable=True))
        db.session.commit()
        yield app, stats, gateway
        db.session.remove()
        db.drop_all()


def client():
    return Client.query.filter_by(email="person").one()


def set_counters(gateway, up, down):
    identity = build_runtime_email("ib", "person")
    gateway.values[f"user>>>{identity}>>>traffic>>>uplink"] = up
    gateway.values[f"user>>>{identity}>>>traffic>>>downlink"] = down


def test_failed_traffic_commit_does_not_destroy_runtime_counters(traffic, monkeypatch):
    app, stats, gateway = traffic
    set_counters(gateway, 50, 70)
    original = db.session.commit
    monkeypatch.setattr(db.session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))
    with pytest.raises(RuntimeError):
        stats.sync_traffic_stats()
    db.session.rollback()
    monkeypatch.setattr(db.session, "commit", original)
    stats.sync_traffic_stats()
    assert (client().up, client().down) == (50, 70)
    stats.sync_traffic_stats()
    assert (client().up, client().down) == (50, 70)


def test_runtime_epoch_change_counts_new_raw_values(traffic):
    app, stats, gateway = traffic
    set_counters(gateway, 50, 70)
    stats.sync_traffic_stats()
    gateway.epoch = "epoch-two"
    set_counters(gateway, 80, 100)
    stats.sync_traffic_stats()
    assert (client().up, client().down) == (130, 170)


def test_reset_fences_old_bytes_and_opens_notification_cycle(traffic):
    app, stats, gateway = traffic
    client().up = 100
    client().telegram_id = 1
    db.session.add(NotificationLog(telegram_id=1, client_id=client().id, kind="traffic_80"))
    db.session.commit()
    set_counters(gateway, 20, 30)
    traffic_store.reset_user_traffic("ib", "person")
    set_counters(gateway, 25, 37)
    stats.sync_traffic_stats()
    assert (client().up, client().down) == (5, 7)
    assert NotificationLog.query.count() == 0
    assert client().last_reset_time > 0
    assert client().traffic_generation


def test_reset_failure_does_not_zero_database(traffic, monkeypatch):
    app, stats, gateway = traffic
    client().up = 100
    db.session.commit()
    monkeypatch.setattr(
        gateway, "read_traffic_counters", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("RPC unavailable"))
    )
    with pytest.raises(RuntimeError):
        traffic_store.reset_user_traffic("ib", "person")
    db.session.rollback()
    assert client().up == 100


def test_due_monthly_reset_precedes_quota_enforcement(traffic):
    app, stats, gateway = traffic
    client().reset_day = datetime.now().day
    client().limit_bytes = client().up = 100
    db.session.commit()
    stats.check_limits_and_reset()
    assert client().enable is True
    assert client().up == 0


def test_corrupt_row_cannot_stop_other_expiry_enforcement(traffic):
    app, stats, gateway = traffic
    client().expiry_time = None
    db.session.add(Client(id=str(uuid.uuid4()), email="expired", inbound_tag="ib", expiry_time=1, enable=True))
    db.session.commit()
    stats.check_limits_and_reset()
    assert Client.query.filter_by(email="expired").one().enable is False
    assert client().enable is False
    assert client().disable_reason == "invalid"


def test_renewal_during_notification_cannot_be_overwritten_by_enforcement(traffic, monkeypatch):
    app, stats, gateway = traffic
    from panel_core.services import notifications

    client().telegram_id = 1
    client().expiry_time = 1
    db.session.commit()

    def renew(*args):
        client().expiry_time = int((datetime.now() + timedelta(days=30)).timestamp() * 1000)
        client().enable = True
        client().access_generation = "renewed"
        db.session.commit()
        gateway.active.add("person")

    monkeypatch.setattr(notifications, "emit_if_new", renew)
    stats.check_limits_and_reset()
    assert client().enable is True
    assert "person" in gateway.active


def test_log_checkpoint_rolls_back_with_ingested_rows(traffic, monkeypatch):
    app, stats, gateway = traffic
    identity = build_runtime_email("ib", "person")
    Path(stats.ACCESS_LOG_PATH).write_text(
        f"2026/09/13 12:00:00 192.0.2.4:4000 accepted tcp:example.org:443 email: {identity}\n"
    )
    original = db.session.commit
    monkeypatch.setattr(db.session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))
    try:
        stats._parse_access_logs_logic()
    except RuntimeError:
        pass
    db.session.rollback()
    monkeypatch.setattr(db.session, "commit", original)
    stats._parse_access_logs_logic()
    assert DomainStat.query.one().hit_count == 1


def test_ipv6_access_logs_and_incomplete_tail(traffic):
    app, stats, gateway = traffic
    identity = build_runtime_email("ib", "person")
    line = f"2026/09/13 12:00:00 [2001:db8::1]:4000 accepted tcp:example.org:443 email: {identity}"
    Path(stats.ACCESS_LOG_PATH).write_text(line)
    stats._parse_access_logs_logic()
    assert DomainStat.query.count() == 0
    Path(stats.ACCESS_LOG_PATH).write_text(line + "\n")
    stats._parse_access_logs_logic()
    assert DomainStat.query.one().hit_count == 1
    assert json.loads(client().source_ips) == ["2001:db8::1"]


def test_delayed_sample_cannot_cross_traffic_generation(traffic):
    app, stats, gateway = traffic
    set_counters(gateway, 10, 20)
    sample = traffic_store.read_traffic_sample()
    traffic_store.start_traffic_cycle(client(), sample=sample)
    db.session.commit()
    with pytest.raises(RuntimeError, match="generation changed"):
        traffic_store.settle_client_traffic(client(), sample=sample)
    assert client().up == 0


def test_rotated_larger_log_is_read_from_beginning(traffic):
    app, stats, gateway = traffic
    identity = build_runtime_email("ib", "person")
    path = Path(stats.ACCESS_LOG_PATH)
    line = f"192.0.2.1:1 accepted tcp:example.org:443 email: {identity}\n"
    path.write_text(line)
    stats._parse_access_logs_logic()
    path.rename(path.with_suffix(".old"))
    path.write_text(line + line)
    stats._parse_access_logs_logic()
    assert DomainStat.query.one().hit_count == 3


def test_custom_domain_range_excludes_following_midnight(traffic):
    from panel_core.api.statistics import _resolve_range, _top_domains

    traffic_store._upsert_domain_stat("2026-09-12", "first.example", "person", "ib", 1)
    traffic_store._upsert_domain_stat("2026-09-13", "second.example", "person", "ib", 1)
    since = int(datetime(2026, 9, 12).timestamp())
    until = int(datetime(2026, 9, 13).timestamp())
    _, start, _, end = _resolve_range({"from": since, "to": until})
    assert [row.domain for row in _top_domains(start, end)] == ["first.example"]


def test_planned_restart_settles_available_counters(traffic):
    from panel_core.services import runtime_apply

    app, stats, gateway = traffic
    set_counters(gateway, 23, 42)
    revision = runtime_apply.mark_runtime_dirty()
    db.session.commit()
    runtime_apply.synchronize_runtime(expected_revision=revision)
    assert (client().up, client().down) == (23, 42)
