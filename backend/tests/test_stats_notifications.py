import time
import uuid
from unittest.mock import MagicMock, patch

from panel_core.extensions import db
from panel_core.models import Client, Inbound
from panel_core.services.stats import check_limits_and_reset, sync_traffic_stats


def _inbound():
    db.session.add(Inbound(tag="DE-vless", protocol="vless", port=10001, stream_settings="{}"))
    db.session.commit()


def _stub(value):
    stub = MagicMock()

    def _q(*_a, **_k):
        resp = MagicMock()
        leaf = MagicMock()
        leaf.value = value
        resp.stat = [leaf]
        return resp

    stub.QueryStats.side_effect = _q
    return stub


def test_sync_traffic_emits_notification_at_80pct(app):
    with app.app_context():
        _inbound()
        c = Client(
            id=str(uuid.uuid4()),
            email="u1",
            inbound_tag="DE-vless",
            telegram_id=55,
            limit_bytes=100,
            up=0,
            down=0,
            enable=True,
            expiry_time=0,
        )
        db.session.add(c)
        db.session.commit()
        with (
            patch("panel_core.services.stats.get_channel", return_value=MagicMock()),
            patch("panel_core.services.stats.stats_command_pb2_grpc.StatsServiceStub", return_value=_stub(40)),
            patch("panel_core.jobs.notifications.bot_events.publish") as mock_publish,
        ):
            sync_traffic_stats()

        assert mock_publish.call_count == 1
        event_type, tg_id, payload = mock_publish.call_args.args
        assert event_type == "traffic_notification"
        assert tg_id == 55
        assert payload["kind"] == "traffic_80"
        assert payload["pct"] == 0.8
        assert payload["limit_kind"] == "per_inbound"


def test_sync_traffic_no_notification_below_threshold(app):
    with app.app_context():
        _inbound()
        c = Client(
            id=str(uuid.uuid4()),
            email="u2",
            inbound_tag="DE-vless",
            telegram_id=55,
            limit_bytes=100,
            up=0,
            down=0,
            enable=True,
            expiry_time=0,
        )
        db.session.add(c)
        db.session.commit()
        with (
            patch("panel_core.services.stats.get_channel", return_value=MagicMock()),
            patch("panel_core.services.stats.stats_command_pb2_grpc.StatsServiceStub", return_value=_stub(10)),
            patch("panel_core.jobs.notifications.bot_events.publish") as mock_publish,
        ):
            sync_traffic_stats()
        mock_publish.assert_not_called()


def test_sync_traffic_notification_failure_does_not_break_accounting(app):
    with app.app_context():
        _inbound()
        c = Client(
            id="cli-iso",
            email="u3",
            inbound_tag="DE-vless",
            telegram_id=55,
            limit_bytes=100,
            up=0,
            down=0,
            enable=True,
            expiry_time=0,
        )
        db.session.add(c)
        db.session.commit()
        with (
            patch("panel_core.services.stats.get_channel", return_value=MagicMock()),
            patch("panel_core.services.stats.stats_command_pb2_grpc.StatsServiceStub", return_value=_stub(40)),
            patch("panel_core.jobs.notifications.bot_events.publish", side_effect=RuntimeError("redis down")),
        ):
            sync_traffic_stats()
        db.session.expire_all()
        updated = db.session.get(Client, "cli-iso")
        assert (updated.up + updated.down) == 80


def test_check_limits_emits_expired_and_disables(app):
    with app.app_context():
        _inbound()
        now_ms = int(time.time() * 1000)
        c = Client(
            id="cli-exp",
            email="e1",
            inbound_tag="DE-vless",
            telegram_id=77,
            limit_bytes=0,
            up=0,
            down=0,
            enable=True,
            expiry_time=now_ms - 10_000,
            reset_day=0,
        )
        db.session.add(c)
        db.session.commit()
        with (
            patch("panel_core.services.stats.get_channel", return_value=MagicMock()),
            patch("panel_core.services.stats._api_remove_user_grpc", return_value=True),
            patch("panel_core.services.stats.generate_config_file"),
            patch("panel_core.services.stats.restart_xray_container"),
            patch("panel_core.jobs.notifications.bot_events.publish") as mock_publish,
        ):
            check_limits_and_reset()
        db.session.expire_all()
        assert db.session.get(Client, "cli-exp").enable is False
        kinds = [call.args[2]["kind"] for call in mock_publish.call_args_list]
        assert "expired" in kinds
        assert mock_publish.call_args_list[0].args[0] == "expiry_notification"


def test_check_limits_emits_3d_warning_without_disabling(app):
    with app.app_context():
        _inbound()
        now_ms = int(time.time() * 1000)
        c = Client(
            id="cli-3d",
            email="w1",
            inbound_tag="DE-vless",
            telegram_id=88,
            limit_bytes=0,
            up=0,
            down=0,
            enable=True,
            expiry_time=now_ms + 2 * 86400 * 1000,
            reset_day=0,
        )
        db.session.add(c)
        db.session.commit()
        with (
            patch("panel_core.services.stats.get_channel", return_value=MagicMock()),
            patch("panel_core.services.stats._api_remove_user_grpc", return_value=True),
            patch("panel_core.services.stats.generate_config_file"),
            patch("panel_core.services.stats.restart_xray_container"),
            patch("panel_core.jobs.notifications.bot_events.publish") as mock_publish,
        ):
            check_limits_and_reset()
        db.session.expire_all()
        assert db.session.get(Client, "cli-3d").enable is True
        assert mock_publish.call_count == 1
        assert mock_publish.call_args.args[2]["kind"] == "expiry_3d"
