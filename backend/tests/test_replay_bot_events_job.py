import datetime as dt
from unittest.mock import MagicMock, patch

from panel_core.models import BotEvent


def _aged(seconds_old: int) -> dt.datetime:
    return dt.datetime.utcnow() - dt.timedelta(seconds=seconds_old)


def test_replay_picks_up_old_undelivered_and_marks_them(app, db):

    from panel_core.jobs.notifications import replay_undelivered_bot_events

    event = BotEvent(type="payment_succeeded", telegram_id=42, payload={})
    db.session.add(event)
    db.session.flush()
    event.created_at = _aged(120)
    db.session.commit()

    fake_redis = MagicMock()
    with patch("panel_core.jobs.notifications._get_redis", return_value=fake_redis):
        replay_undelivered_bot_events()

    fake_redis.publish.assert_called_once()
    refreshed = db.session.get(BotEvent, event.id)
    assert refreshed.delivered_at is not None


def test_replay_skips_recent_events_to_avoid_racing_publish(app, db):

    from panel_core.jobs.notifications import replay_undelivered_bot_events

    db.session.add(BotEvent(type="payment_succeeded", telegram_id=42, payload={}))
    db.session.commit()

    fake_redis = MagicMock()
    with patch("panel_core.jobs.notifications._get_redis", return_value=fake_redis):
        replay_undelivered_bot_events()

    fake_redis.publish.assert_not_called()


def test_replay_skips_already_delivered(app, db):

    from panel_core.jobs.notifications import replay_undelivered_bot_events

    e = BotEvent(type="t", telegram_id=42, payload={})
    db.session.add(e)
    db.session.flush()
    e.created_at = _aged(120)
    e.delivered_at = dt.datetime.utcnow()
    db.session.commit()

    fake_redis = MagicMock()
    with patch("panel_core.jobs.notifications._get_redis", return_value=fake_redis):
        replay_undelivered_bot_events()

    fake_redis.publish.assert_not_called()


def test_replay_keeps_delivered_at_null_when_publish_fails(app, db):

    from panel_core.jobs.notifications import replay_undelivered_bot_events

    e = BotEvent(type="payment_succeeded", telegram_id=42, payload={})
    db.session.add(e)
    db.session.flush()
    e.created_at = _aged(120)
    db.session.commit()

    fake_redis = MagicMock()
    fake_redis.publish.side_effect = ConnectionError("redis down")
    with patch("panel_core.jobs.notifications._get_redis", return_value=fake_redis):
        replay_undelivered_bot_events()

    refreshed = db.session.get(BotEvent, e.id)
    assert refreshed.delivered_at is None


def test_replay_noop_when_redis_unconfigured(app, db):

    from panel_core.jobs.notifications import replay_undelivered_bot_events

    e = BotEvent(type="t", telegram_id=42, payload={})
    db.session.add(e)
    db.session.flush()
    e.created_at = _aged(120)
    db.session.commit()

    with patch("panel_core.jobs.notifications._get_redis", return_value=None):
        replay_undelivered_bot_events()

    refreshed = db.session.get(BotEvent, e.id)
    assert refreshed.delivered_at is None
