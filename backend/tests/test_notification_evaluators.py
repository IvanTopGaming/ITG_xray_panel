from types import SimpleNamespace

from app.jobs.notifications import evaluate_traffic


def _c(**kw):
    base = dict(limit_bytes=0, up=0, down=0, expiry_time=0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_traffic_none_when_no_limit():
    assert evaluate_traffic(_c(limit_bytes=0, up=999, down=999)) is None


def test_traffic_below_80_returns_none():
    assert evaluate_traffic(_c(limit_bytes=100, up=40, down=39)) is None


def test_traffic_80_bucket():
    assert evaluate_traffic(_c(limit_bytes=100, up=80, down=0)) == "traffic_80"


def test_traffic_95_bucket():
    assert evaluate_traffic(_c(limit_bytes=100, up=95, down=0)) == "traffic_95"


def test_traffic_exhausted_bucket():
    assert evaluate_traffic(_c(limit_bytes=100, up=100, down=0)) == "traffic_exhausted"


def test_traffic_returns_highest_crossed():
    assert evaluate_traffic(_c(limit_bytes=100, up=96, down=0)) == "traffic_95"


from app.jobs.notifications import evaluate_expiry

_DAY = 86400 * 1000
_HOUR = 3600 * 1000


def test_expiry_none_when_zero():
    assert evaluate_expiry(_c(expiry_time=0), 1_000_000) is None


def test_expiry_none_when_far_future():
    now = 1_000_000
    assert evaluate_expiry(_c(expiry_time=now + 5 * _DAY), now) is None


def test_expiry_3d_bucket():
    now = 1_000_000
    assert evaluate_expiry(_c(expiry_time=now + 2 * _DAY), now) == "expiry_3d"


def test_expiry_boundary_exactly_3d_is_3d():
    now = 1_000_000
    assert evaluate_expiry(_c(expiry_time=now + 3 * _DAY), now) == "expiry_3d"


def test_expiry_1d_bucket():
    now = 1_000_000
    assert evaluate_expiry(_c(expiry_time=now + 12 * _HOUR), now) == "expiry_1d"


def test_expiry_1h_bucket():
    now = 1_000_000
    assert evaluate_expiry(_c(expiry_time=now + 30 * 60 * 1000), now) == "expiry_1h"


def test_expired_bucket():
    now = 1_000_000
    assert evaluate_expiry(_c(expiry_time=now - 5 * 60 * 1000), now) == "expired"


from unittest.mock import patch

from app.extensions import db
from app.models import Client, Inbound, NotificationLog
from app.jobs.notifications import emit_if_new


def test_emit_if_new_publishes_once_then_dedups(app):
    with app.app_context():
        db.session.add(Inbound(tag="vless-de", protocol="vless", port=443, stream_settings="{}"))
        c = Client(
            id="cli-x",
            email="x",
            inbound_tag="vless-de",
            telegram_id=7,
            tariff_id=None,
            expiry_time=0,
            enable=True,
            up=0,
            down=0,
            limit_bytes=0,
        )
        db.session.add(c)
        db.session.commit()

        with patch("app.jobs.notifications.bot_events.publish") as mock_publish:
            r1 = emit_if_new("expiry_notification", "expired", c, {"expiry_time_ms": c.expiry_time})
            r2 = emit_if_new("expiry_notification", "expired", c, {"expiry_time_ms": c.expiry_time})

        assert r1 is True
        assert r2 is False
        assert mock_publish.call_count == 1
        event_type, tg_id, payload = mock_publish.call_args.args
        assert event_type == "expiry_notification"
        assert tg_id == 7
        assert payload["kind"] == "expired"
        assert payload["client_id"] == "cli-x"
        assert payload["email"] == "x"
        assert payload["expiry_time_ms"] == 0
        assert payload["tariff_id"] is None
        assert payload["renewable"] is False
        assert payload["lang"] == "ru"
        assert NotificationLog.query.filter_by(client_id="cli-x", kind="expired").count() == 1
