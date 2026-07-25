from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, mock_open

import pytest

from panel_core.models import Client, DomainStat, Inbound, NotificationLog
from sqlalchemy import event
from sqlalchemy.engine import Engine

from panel_core.services.stats import (
    _is_ip_address,
    _ten_min_bucket,
    check_limits_and_reset,
    cleanup_old_domain_stats,
    reset_user_traffic,
    sync_traffic_stats,
    _parse_access_logs_logic,
    _ACCEPT_FULL,
    _ACCEPT_BASIC,
)


class _SqlOrderRecorder:
    def __init__(self, order: list[str], label: str = "sql_write"):
        self._order = order
        self._label = label

    def _listener(self, _conn, _cur, statement, *_args):
        head = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if head in ("INSERT", "UPDATE", "DELETE"):
            self._order.append(self._label)

    def __enter__(self):
        event.listen(Engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *_exc):
        event.remove(Engine, "before_cursor_execute", self._listener)


def _make_inbound(db_, *, tag="DE-vless", protocol="vless", port=10001):
    ib = Inbound(tag=tag, protocol=protocol, port=port, stream_settings="{}")
    db_.session.add(ib)
    db_.session.flush()
    return ib


def _make_client(
    db_,
    *,
    inbound_tag="DE-vless",
    email=None,
    limit_bytes=0,
    expiry_time=0,
    up=0,
    down=0,
    enable=True,
    reset_day=0,
    last_reset_time=0,
    telegram_id=None,
):
    if email is None:
        email = f"user_{uuid.uuid4().hex[:6]}"
    c = Client(
        id=str(uuid.uuid4()),
        email=email,
        inbound_tag=inbound_tag,
        limit_bytes=limit_bytes,
        expiry_time=expiry_time,
        up=up,
        down=down,
        enable=enable,
        reset_day=reset_day,
        last_reset_time=last_reset_time,
        telegram_id=telegram_id,
    )
    db_.session.add(c)
    db_.session.commit()
    return c


class TestTenMinBucket:
    def test_rounds_down_to_ten_minute_boundary(self):
        dt = datetime(2025, 6, 15, 14, 37, 59)
        bucket = _ten_min_bucket(dt)
        expected = datetime(2025, 6, 15, 14, 30, 0)
        assert bucket == int(expected.timestamp())

    def test_exact_boundary_stays_unchanged(self):
        dt = datetime(2025, 6, 15, 14, 20, 0)
        bucket = _ten_min_bucket(dt)
        assert bucket == int(dt.timestamp())

    def test_minute_zero(self):
        dt = datetime(2025, 6, 15, 14, 0, 45)
        bucket = _ten_min_bucket(dt)
        expected = datetime(2025, 6, 15, 14, 0, 0)
        assert bucket == int(expected.timestamp())

    def test_minute_59(self):
        dt = datetime(2025, 6, 15, 14, 59, 59)
        bucket = _ten_min_bucket(dt)
        expected = datetime(2025, 6, 15, 14, 50, 0)
        assert bucket == int(expected.timestamp())


class TestIsIpAddress:
    def test_valid_ipv4(self):
        assert _is_ip_address("192.168.1.1") is True

    def test_valid_ipv6(self):
        assert _is_ip_address("::1") is True
        assert _is_ip_address("2001:db8::1") is True

    def test_domain_is_not_ip(self):
        assert _is_ip_address("example.com") is False

    def test_empty_string(self):
        assert _is_ip_address("") is False

    def test_partial_ip(self):
        assert _is_ip_address("192.168") is False

    def test_ip_with_port_is_not_ip(self):
        assert _is_ip_address("192.168.1.1:8080") is False


_GRPC_PATCHES = {
    "get_channel": "panel_core.services.stats.get_channel",
    "remove_grpc": "panel_core.services.stats._api_remove_user_grpc",
    "gen_config": "panel_core.services.stats.generate_config_file",
    "restart": "panel_core.services.stats.restart_xray_container",
}


class TestCheckLimitsOverTraffic:
    def test_disables_client_when_traffic_exceeds_limit(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        client = _make_client(
            db,
            inbound_tag="DE-vless",
            limit_bytes=1_000_000,
            up=600_000,
            down=500_000,
            enable=True,
        )
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True),
            patch(_GRPC_PATCHES["gen_config"]),
            patch(_GRPC_PATCHES["restart"]),
        ):
            check_limits_and_reset()

        db.session.refresh(client)
        assert client.enable is False

    def test_disables_client_when_traffic_equals_limit(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        client = _make_client(
            db,
            inbound_tag="DE-vless",
            limit_bytes=1_000_000,
            up=500_000,
            down=500_000,
            enable=True,
        )
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True),
            patch(_GRPC_PATCHES["gen_config"]),
            patch(_GRPC_PATCHES["restart"]),
        ):
            check_limits_and_reset()

        db.session.refresh(client)
        assert client.enable is False


class TestCheckLimitsExpired:
    def test_disables_client_when_expired(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        past_ts = int((time.time() - 3600) * 1000)
        client = _make_client(
            db,
            inbound_tag="DE-vless",
            expiry_time=past_ts,
            enable=True,
        )
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True),
            patch(_GRPC_PATCHES["gen_config"]),
            patch(_GRPC_PATCHES["restart"]),
        ):
            check_limits_and_reset()

        db.session.refresh(client)
        assert client.enable is False


class TestCheckLimitsHealthy:
    def test_does_not_disable_healthy_client(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        future_ts = int((time.time() + 86400) * 1000)
        client = _make_client(
            db,
            inbound_tag="DE-vless",
            limit_bytes=10_000_000,
            up=1_000_000,
            down=2_000_000,
            expiry_time=future_ts,
            enable=True,
        )
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True),
            patch(_GRPC_PATCHES["gen_config"]),
            patch(_GRPC_PATCHES["restart"]),
        ):
            check_limits_and_reset()

        db.session.refresh(client)
        assert client.enable is True

    def test_does_not_disable_unlimited_client(self, app, db):

        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        client = _make_client(
            db,
            inbound_tag="DE-vless",
            limit_bytes=0,
            up=999_999_999,
            down=999_999_999,
            expiry_time=0,
            enable=True,
        )
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True),
            patch(_GRPC_PATCHES["gen_config"]),
            patch(_GRPC_PATCHES["restart"]),
        ):
            check_limits_and_reset()

        db.session.refresh(client)
        assert client.enable is True

    def test_expired_zero_means_no_expiry(self, app, db):

        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        client = _make_client(
            db,
            inbound_tag="DE-vless",
            expiry_time=0,
            limit_bytes=0,
            enable=True,
        )
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True),
            patch(_GRPC_PATCHES["gen_config"]),
            patch(_GRPC_PATCHES["restart"]),
        ):
            check_limits_and_reset()

        db.session.refresh(client)
        assert client.enable is True


class TestCheckLimitsMonthlyReset:
    def test_resets_counters_on_reset_day(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        current_day = datetime.now().day

        yesterday_ts = int((datetime.now() - timedelta(days=1)).timestamp() * 1000)
        client = _make_client(
            db,
            inbound_tag="DE-vless",
            limit_bytes=10_000_000_000,
            up=5_000_000_000,
            down=3_000_000_000,
            enable=True,
            reset_day=current_day,
            last_reset_time=yesterday_ts,
        )

        mock_stub = MagicMock()
        mock_channel = MagicMock()
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=mock_channel),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True),
            patch(_GRPC_PATCHES["gen_config"]),
            patch(_GRPC_PATCHES["restart"]),
            patch(
                "panel_core.services.stats.stats_command_pb2_grpc.StatsServiceStub",
                return_value=mock_stub,
            ),
        ):
            check_limits_and_reset()

        db.session.refresh(client)
        assert client.up == 0
        assert client.down == 0
        assert client.last_reset_time > 0

    def test_does_not_reset_if_already_reset_today(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        current_day = datetime.now().day

        now_ts = int(datetime.now().timestamp() * 1000)
        client = _make_client(
            db,
            inbound_tag="DE-vless",
            limit_bytes=10_000_000_000,
            up=5_000_000_000,
            down=3_000_000_000,
            enable=True,
            reset_day=current_day,
            last_reset_time=now_ts,
        )

        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True),
            patch(_GRPC_PATCHES["gen_config"]),
            patch(_GRPC_PATCHES["restart"]),
        ):
            check_limits_and_reset()

        db.session.refresh(client)

        assert client.up == 5_000_000_000
        assert client.down == 3_000_000_000

    def test_does_not_reset_on_wrong_day(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        current_day = datetime.now().day
        wrong_day = (current_day % 28) + 1
        client = _make_client(
            db,
            inbound_tag="DE-vless",
            limit_bytes=10_000_000_000,
            up=5_000_000_000,
            down=3_000_000_000,
            enable=True,
            reset_day=wrong_day,
            last_reset_time=0,
        )

        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True),
            patch(_GRPC_PATCHES["gen_config"]),
            patch(_GRPC_PATCHES["restart"]),
        ):
            check_limits_and_reset()

        db.session.refresh(client)
        assert client.up == 5_000_000_000
        assert client.down == 3_000_000_000


class TestCheckLimitsClearsNotifications:
    def test_clears_traffic_notifications_on_monthly_reset(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        current_day = datetime.now().day
        yesterday_ts = int((datetime.now() - timedelta(days=1)).timestamp() * 1000)
        client = _make_client(
            db,
            inbound_tag="DE-vless",
            limit_bytes=10_000_000_000,
            up=5_000_000_000,
            down=3_000_000_000,
            enable=True,
            reset_day=current_day,
            last_reset_time=yesterday_ts,
            telegram_id=42,
        )

        db.session.add_all(
            [
                NotificationLog(telegram_id=42, client_id=client.id, kind="traffic_80"),
                NotificationLog(telegram_id=42, client_id=client.id, kind="traffic_95"),
                NotificationLog(telegram_id=42, client_id=client.id, kind="traffic_exhausted"),
                NotificationLog(telegram_id=42, client_id=client.id, kind="expiry_1d"),
            ]
        )
        db.session.commit()

        mock_stub = MagicMock()
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True),
            patch(_GRPC_PATCHES["gen_config"]),
            patch(_GRPC_PATCHES["restart"]),
            patch(
                "panel_core.services.stats.stats_command_pb2_grpc.StatsServiceStub",
                return_value=mock_stub,
            ),
        ):
            check_limits_and_reset()

        remaining = NotificationLog.query.filter_by(client_id=client.id).all()
        kinds = {n.kind for n in remaining}
        assert "traffic_80" not in kinds
        assert "traffic_95" not in kinds
        assert "traffic_exhausted" not in kinds

        assert "expiry_1d" in kinds


class TestParseAccessLogs:
    def test_full_regex_matches_valid_line(self):
        line = (
            "2025/06/15 14:30:00 192.168.1.10:54321 accepted "
            "tcp:example.com:443 [inbound >> tag] email: v1|DE-vless|dXNlcl8x"
        )
        m = _ACCEPT_FULL.search(line)
        assert m is not None
        assert m.group(1) == "192.168.1.10"
        assert m.group(2) == "example.com"
        assert m.group(3) == "v1|DE-vless|dXNlcl8x"

    def test_basic_regex_matches_line_without_destination(self):
        line = "2025/06/15 14:30:00 10.0.0.1:12345 accepted something email: testuser"
        m = _ACCEPT_BASIC.search(line)
        assert m is not None
        assert m.group(1) == "10.0.0.1"
        assert m.group(2) == "testuser"

    def test_full_regex_skips_bare_ip_destination(self):

        line = (
            "2025/06/15 14:30:00 192.168.1.10:54321 accepted "
            "tcp:93.184.216.34:443 [inbound] email: v1|DE-vless|dXNlcl8x"
        )
        m = _ACCEPT_FULL.search(line)
        assert m is not None
        dest = m.group(2)

        assert _is_ip_address(dest) is True

    def test_parse_access_logs_upserts_domain_stats(self, app, db):

        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        _make_client(
            db,
            inbound_tag="DE-vless",
            email="user1",
            enable=True,
        )

        from panel_core.services.runtime_identity import build_runtime_email

        runtime = build_runtime_email("DE-vless", "user1")
        log_line = f"2025/06/15 14:30:00 192.168.1.10:54321 accepted tcp:example.com:443 [DE-vless] email: {runtime}\n"

        with (
            patch("panel_core.services.stats.os.path.exists", return_value=True),
            patch("panel_core.services.stats.os.path.getsize", return_value=len(log_line)),
            patch("panel_core.services.stats._read_access_offset", return_value=0),
            patch("panel_core.services.stats._write_access_offset"),
            patch("builtins.open", mock_open(read_data=log_line)),
        ):
            _parse_access_logs_logic()

        stats = DomainStat.query.filter_by(domain="example.com").all()
        assert len(stats) == 1
        assert stats[0].client_email == "user1"
        assert stats[0].inbound_tag == "DE-vless"
        assert stats[0].hit_count >= 1

    def test_parse_access_logs_skips_bare_ip_destinations(self, app, db):

        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        _make_client(
            db,
            inbound_tag="DE-vless",
            email="user1",
            enable=True,
        )

        from panel_core.services.runtime_identity import build_runtime_email

        runtime = build_runtime_email("DE-vless", "user1")
        log_line = (
            f"2025/06/15 14:30:00 192.168.1.10:54321 accepted tcp:93.184.216.34:443 [DE-vless] email: {runtime}\n"
        )

        with (
            patch("panel_core.services.stats.os.path.exists", return_value=True),
            patch("panel_core.services.stats.os.path.getsize", return_value=len(log_line)),
            patch("panel_core.services.stats._read_access_offset", return_value=0),
            patch("panel_core.services.stats._write_access_offset"),
            patch("builtins.open", mock_open(read_data=log_line)),
        ):
            _parse_access_logs_logic()

        stats = DomainStat.query.all()
        assert len(stats) == 0


class TestCleanupOldDomainStats:
    def test_deletes_rows_older_than_90_days(self, app, db):
        old_date = (datetime.now() - timedelta(days=100)).date().isoformat()
        recent_date = (datetime.now() - timedelta(days=10)).date().isoformat()
        db.session.add_all(
            [
                DomainStat(date=old_date, domain="old.com", client_email="u1", inbound_tag="t1", hit_count=5),
                DomainStat(date=recent_date, domain="new.com", client_email="u2", inbound_tag="t2", hit_count=3),
            ]
        )
        db.session.commit()

        cleanup_old_domain_stats()

        remaining = DomainStat.query.all()
        assert len(remaining) == 1
        assert remaining[0].domain == "new.com"

    def test_does_nothing_when_no_old_rows(self, app, db):
        recent_date = datetime.now().date().isoformat()
        db.session.add(
            DomainStat(date=recent_date, domain="fresh.com", client_email="u1", inbound_tag="t1", hit_count=1)
        )
        db.session.commit()

        cleanup_old_domain_stats()

        assert DomainStat.query.count() == 1


class TestResetUserTraffic:
    def test_zeroes_client_counters(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        client = _make_client(
            db,
            inbound_tag="DE-vless",
            email="myuser",
            up=5_000_000,
            down=3_000_000,
        )

        mock_stub = MagicMock()
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(
                "panel_core.services.stats.stats_command_pb2_grpc.StatsServiceStub",
                return_value=mock_stub,
            ),
        ):
            reset_user_traffic("DE-vless", "myuser")

        db.session.refresh(client)
        assert client.up == 0
        assert client.down == 0

    def test_raises_when_user_not_found(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        with pytest.raises(Exception, match="User not found"):
            reset_user_traffic("DE-vless", "nonexistent")


class TestCheckLimitsXrayInteraction:
    def test_calls_generate_config_on_disable(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        past_ts = int((time.time() - 3600) * 1000)
        _make_client(
            db,
            inbound_tag="DE-vless",
            expiry_time=past_ts,
            enable=True,
        )
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True) as mock_remove,
            patch(_GRPC_PATCHES["gen_config"]) as mock_gen,
            patch(_GRPC_PATCHES["restart"]) as mock_restart,
        ):
            check_limits_and_reset()

        mock_gen.assert_called_once()

        mock_remove.assert_called_once()
        mock_restart.assert_not_called()

    def test_restarts_when_grpc_remove_fails(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        past_ts = int((time.time() - 3600) * 1000)
        _make_client(
            db,
            inbound_tag="DE-vless",
            expiry_time=past_ts,
            enable=True,
        )
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=False),
            patch(_GRPC_PATCHES["gen_config"]) as mock_gen,
            patch(_GRPC_PATCHES["restart"]) as mock_restart,
        ):
            check_limits_and_reset()

        mock_gen.assert_called_once()
        mock_restart.assert_called_once()

    def test_restarts_for_non_vless_vmess_protocol(self, app, db):

        _make_inbound(db, tag="TR-trojan", protocol="trojan", port=10002)
        past_ts = int((time.time() - 3600) * 1000)
        _make_client(
            db,
            inbound_tag="TR-trojan",
            expiry_time=past_ts,
            enable=True,
        )
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"], return_value=True) as mock_remove,
            patch(_GRPC_PATCHES["gen_config"]) as mock_gen,
            patch(_GRPC_PATCHES["restart"]) as mock_restart,
        ):
            check_limits_and_reset()

        mock_gen.assert_called_once()
        mock_remove.assert_not_called()
        mock_restart.assert_called_once()

    def test_no_config_change_when_all_clients_healthy(self, app, db):

        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        future_ts = int((time.time() + 86400) * 1000)
        _make_client(
            db,
            inbound_tag="DE-vless",
            limit_bytes=10_000_000,
            up=1_000,
            down=1_000,
            expiry_time=future_ts,
            enable=True,
            reset_day=0,
        )
        with (
            patch(_GRPC_PATCHES["get_channel"], return_value=MagicMock()),
            patch(_GRPC_PATCHES["remove_grpc"]),
            patch(_GRPC_PATCHES["gen_config"]) as mock_gen,
            patch(_GRPC_PATCHES["restart"]) as mock_restart,
        ):
            check_limits_and_reset()

        mock_gen.assert_not_called()
        mock_restart.assert_not_called()


def _make_stats_stub(per_call_value: int):

    stub = MagicMock()

    def _query(*_args, **_kwargs):
        resp = MagicMock()
        leaf = MagicMock()
        leaf.value = per_call_value
        resp.stat = [leaf]
        return resp

    stub.QueryStats.side_effect = _query
    return stub


class TestSyncTrafficTransactionShape:
    def test_grpc_reads_complete_before_any_upsert(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        _make_client(db, inbound_tag="DE-vless", email="u1", enable=True)
        _make_client(db, inbound_tag="DE-vless", email="u2", enable=True)
        _make_client(db, inbound_tag="DE-vless", email="u3", enable=True)

        call_order: list[str] = []

        def _on_query(*_a, **_kw):
            call_order.append("grpc")
            resp = MagicMock()
            leaf = MagicMock()
            leaf.value = 5_000
            resp.stat = [leaf]
            return resp

        def _on_upsert(*_a, **_kw):
            call_order.append("upsert")

        stub = MagicMock()
        stub.QueryStats.side_effect = _on_query

        with (
            patch("panel_core.services.stats.get_channel", return_value=MagicMock()),
            patch("panel_core.services.stats.stats_command_pb2_grpc.StatsServiceStub", return_value=stub),
            patch("panel_core.services.stats._upsert_snapshot", side_effect=_on_upsert),
        ):
            sync_traffic_stats()

        if "upsert" not in call_order:
            pytest.fail("No upsert calls recorded — test fixture did not exercise the write path")

        first_upsert_idx = call_order.index("upsert")
        last_grpc_idx = max(i for i, op in enumerate(call_order) if op == "grpc")
        assert last_grpc_idx < first_upsert_idx, (
            f"gRPC call at index {last_grpc_idx} ran AFTER first upsert at {first_upsert_idx}. "
            f"This holds the SQLite write lock across gRPC calls and starves other writers. "
            f"Sequence: {call_order}"
        )


class TestSyncTrafficCorrectness:
    def test_accumulates_deltas_into_client_counters(self, app, db):
        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        c = _make_client(db, inbound_tag="DE-vless", email="u1", up=100, down=200, enable=True)

        stub = _make_stats_stub(per_call_value=1000)

        with (
            patch("panel_core.services.stats.get_channel", return_value=MagicMock()),
            patch("panel_core.services.stats.stats_command_pb2_grpc.StatsServiceStub", return_value=stub),
        ):
            sync_traffic_stats()

        db.session.refresh(c)
        assert c.up == 100 + 1000
        assert c.down == 200 + 1000

    def test_accumulates_deltas_into_inbound_counters(self, app, db):
        ib = _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        ib.up = 50
        ib.down = 75
        db.session.commit()

        stub = _make_stats_stub(per_call_value=2000)

        with (
            patch("panel_core.services.stats.get_channel", return_value=MagicMock()),
            patch("panel_core.services.stats.stats_command_pb2_grpc.StatsServiceStub", return_value=stub),
        ):
            sync_traffic_stats()

        db.session.refresh(ib)
        assert ib.up == 50 + 2000
        assert ib.down == 75 + 2000

    def test_no_clients_no_inbounds_returns_early(self, app, db):
        with (
            patch("panel_core.services.stats.get_channel") as mock_channel,
            patch("panel_core.services.stats.stats_command_pb2_grpc.StatsServiceStub") as mock_stub_cls,
        ):
            sync_traffic_stats()

        mock_channel.assert_not_called()
        mock_stub_cls.assert_not_called()


class TestCheckLimitsTransactionShape:
    def test_grpc_calls_precede_all_sql_writes(self, app, db):

        _make_inbound(db, tag="DE-vless", protocol="vless", port=10001)
        over_limit = _make_client(
            db,
            inbound_tag="DE-vless",
            limit_bytes=1_000_000,
            up=600_000,
            down=500_000,
            enable=True,
        )
        today_day = datetime.now().day
        on_reset_day = _make_client(
            db,
            inbound_tag="DE-vless",
            limit_bytes=10_000_000_000,
            up=1_000,
            down=1_000,
            enable=True,
            reset_day=today_day,
            last_reset_time=0,
        )

        assert over_limit.id != on_reset_day.id

        order: list[str] = []

        def _on_grpc_query(*_a, **_kw):
            order.append("grpc")
            resp = MagicMock()
            resp.stat = []
            return resp

        def _on_grpc_remove(*_a, **_kw):
            order.append("grpc")
            return True

        stub = MagicMock()
        stub.QueryStats.side_effect = _on_grpc_query

        with (
            _SqlOrderRecorder(order),
            patch("panel_core.services.stats.get_channel", return_value=MagicMock()),
            patch("panel_core.services.stats.stats_command_pb2_grpc.StatsServiceStub", return_value=stub),
            patch("panel_core.services.stats._api_remove_user_grpc", side_effect=_on_grpc_remove),
            patch("panel_core.services.stats.generate_config_file"),
            patch("panel_core.services.stats.restart_xray_container"),
        ):
            check_limits_and_reset()

        grpc_indices = [i for i, op in enumerate(order) if op == "grpc"]
        write_indices = [i for i, op in enumerate(order) if op == "sql_write"]
        assert grpc_indices, f"No gRPC calls recorded — fixture didn't exercise gRPC paths. Order: {order}"
        assert write_indices, f"No SQL writes recorded — check_limits should commit Client.enable=False. Order: {order}"
        assert max(grpc_indices) < min(write_indices), (
            f"gRPC call at {max(grpc_indices)} ran after first SQL write at {min(write_indices)}. "
            f"This holds the SQLite write lock across gRPC calls. Order: {order}"
        )
