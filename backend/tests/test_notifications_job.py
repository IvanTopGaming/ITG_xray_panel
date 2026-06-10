from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Client, Inbound, NotificationLog


def _seconds_to_ms(s: float) -> int:
    return int(s * 1000)


def _now_ms() -> int:
    return _seconds_to_ms(time.time())


@pytest.fixture
def inbound(app):
    with app.app_context():
        ib = Inbound(
            tag="vless-de",
            protocol="vless",
            port=443,
            stream_settings="{}",
        )
        db.session.add(ib)
        db.session.commit()
        yield ib.tag


def _make_client(app, *, telegram_id, email, expiry_offset_ms, enable=True, tariff_id=None):

    with app.app_context():
        c = Client(
            id=f"cli-{email}",
            email=email,
            inbound_tag="vless-de",
            telegram_id=telegram_id,
            tariff_id=tariff_id,
            expiry_time=_now_ms() + expiry_offset_ms,
            enable=enable,
            up=0,
            down=0,
            limit_bytes=0,
        )
        db.session.add(c)
        db.session.commit()
        return c.id


def _make_tariff(app, **overrides):

    from app.models import Tariff, TariffItem

    with app.app_context():
        t = Tariff(
            name=overrides.get("name", "Standard"),
            price_rub=overrides.get("price_rub", 100),
            period_days=overrides.get("period_days", 30),
            visibility=overrides.get("visibility", "public"),
            enabled=overrides.get("enabled", True),
            is_trial=overrides.get("is_trial", False),
        )
        db.session.add(t)
        db.session.flush()
        db.session.add(TariffItem(tariff_id=t.id, inbound_tag="vless-de", traffic_gb=0))
        db.session.commit()
        return t.id


def test_publishes_event_in_3d_window(app, inbound):
    tariff_id = _make_tariff(app)
    cid = _make_client(
        app,
        telegram_id=42,
        email="alice",
        expiry_offset_ms=3 * 86400 * 1000,
        tariff_id=tariff_id,
    )
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    assert mock_publish.call_count == 1
    event_type, tg_id, payload = mock_publish.call_args.args
    assert event_type == "expiry_notification"
    assert tg_id == 42
    assert payload["kind"] == "expiry_3d"
    assert payload["client_id"] == cid
    assert payload["email"] == "alice"

    assert payload["tariff_id"] == tariff_id

    assert payload["renewable"] is True


def test_payload_carries_null_tariff_id_for_legacy_clients(app, inbound):

    _make_client(app, telegram_id=42, email="legacy", expiry_offset_ms=3 * 86400 * 1000)
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    assert mock_publish.call_count == 1
    payload = mock_publish.call_args.args[2]
    assert payload["tariff_id"] is None
    assert payload["renewable"] is False


def test_renewable_false_for_archived_tariff(app, inbound):
    tariff_id = _make_tariff(app, visibility="archived")
    _make_client(
        app,
        telegram_id=42,
        email="alice",
        expiry_offset_ms=3 * 86400 * 1000,
        tariff_id=tariff_id,
    )
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    assert mock_publish.call_args.args[2]["renewable"] is False


def test_renewable_false_for_disabled_tariff(app, inbound):
    tariff_id = _make_tariff(app, enabled=False)
    _make_client(
        app,
        telegram_id=42,
        email="alice",
        expiry_offset_ms=3 * 86400 * 1000,
        tariff_id=tariff_id,
    )
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    assert mock_publish.call_args.args[2]["renewable"] is False


def test_renewable_false_for_trial_tariff(app, inbound):
    tariff_id = _make_tariff(app, is_trial=True)
    _make_client(
        app,
        telegram_id=42,
        email="alice",
        expiry_offset_ms=3 * 86400 * 1000,
        tariff_id=tariff_id,
    )
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    assert mock_publish.call_args.args[2]["renewable"] is False


def test_renewable_false_for_private_tariff_without_grant(app, inbound):
    tariff_id = _make_tariff(app, visibility="private")
    _make_client(
        app,
        telegram_id=42,
        email="alice",
        expiry_offset_ms=3 * 86400 * 1000,
        tariff_id=tariff_id,
    )
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    assert mock_publish.call_args.args[2]["renewable"] is False


def test_renewable_true_for_private_tariff_with_grant(app, inbound):
    from app.models import UserTariffAccess

    tariff_id = _make_tariff(app, visibility="private")
    with app.app_context():
        db.session.add(UserTariffAccess(telegram_id=42, tariff_id=tariff_id, billing="paid"))
        db.session.commit()
    _make_client(
        app,
        telegram_id=42,
        email="alice",
        expiry_offset_ms=3 * 86400 * 1000,
        tariff_id=tariff_id,
    )
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    assert mock_publish.call_args.args[2]["renewable"] is True


def test_dedups_within_same_kind(app, inbound):
    _make_client(app, telegram_id=42, email="alice", expiry_offset_ms=3 * 86400 * 1000)
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
        send_expiry_notifications()
    assert mock_publish.call_count == 1, "second run should be a no-op (dedup)"
    with app.app_context():
        assert NotificationLog.query.count() == 1


def test_skips_clients_without_telegram_id(app, inbound):
    _make_client(app, telegram_id=None, email="orphan", expiry_offset_ms=86400 * 1000)
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    mock_publish.assert_not_called()


def test_skips_clients_with_zero_expiry(app, inbound):
    with app.app_context():
        c = Client(
            id="cli-noexpiry",
            email="noexpiry",
            inbound_tag="vless-de",
            telegram_id=42,
            expiry_time=0,
            enable=True,
            up=0,
            down=0,
            limit_bytes=0,
        )
        db.session.add(c)
        db.session.commit()
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    mock_publish.assert_not_called()


def test_skips_disabled_clients(app, inbound):
    _make_client(app, telegram_id=42, email="disabled", expiry_offset_ms=86400 * 1000, enable=False)
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    mock_publish.assert_not_called()


def test_publishes_separate_events_for_3d_1d_1h_buckets(app, inbound):
    _make_client(app, telegram_id=1, email="a", expiry_offset_ms=3 * 86400 * 1000)
    _make_client(app, telegram_id=2, email="b", expiry_offset_ms=1 * 86400 * 1000)
    _make_client(app, telegram_id=3, email="c", expiry_offset_ms=3600 * 1000)
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    kinds = {call.args[2]["kind"] for call in mock_publish.call_args_list}
    assert kinds == {"expiry_3d", "expiry_1d", "expiry_1h"}


def test_expired_kind_for_just_expired_clients(app, inbound):

    _make_client(app, telegram_id=42, email="expired", expiry_offset_ms=-10 * 60 * 1000)
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    assert mock_publish.call_count == 1
    assert mock_publish.call_args.args[2]["kind"] == "expired"


def test_includes_lang_from_telegram_user(app, inbound):

    from app.models import TelegramUser

    with app.app_context():
        db.session.add(TelegramUser(telegram_id=42, language="en"))
        db.session.commit()
    _make_client(app, telegram_id=42, email="alice", expiry_offset_ms=86400 * 1000)
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    assert mock_publish.call_args.args[2]["lang"] == "en"


def test_defaults_lang_to_ru_when_no_telegram_user(app, inbound):
    _make_client(app, telegram_id=42, email="alice", expiry_offset_ms=86400 * 1000)
    from app.jobs.notifications import send_expiry_notifications

    with app.app_context(), patch("app.jobs.notifications.bot_events.publish") as mock_publish:
        send_expiry_notifications()
    assert mock_publish.call_args.args[2]["lang"] == "ru"
