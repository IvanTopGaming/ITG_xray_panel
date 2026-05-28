"""Tests for the replay-undelivered-bot-events cron.

This is the consumer-side of the BotEvent recovery buffer: events that
weren't pushed to Redis in real time (transient Redis outage, race during
publish) sit in `bot_event` with delivered_at=None. The cron picks them up,
re-publishes to Redis, and marks delivered_at on success.
"""

import datetime as dt
from unittest.mock import MagicMock, patch

from app.models import BotEvent


def _aged(seconds_old: int) -> dt.datetime:
    return dt.datetime.utcnow() - dt.timedelta(seconds=seconds_old)


def test_replay_picks_up_old_undelivered_and_marks_them(app, db):
    """An event whose Redis publish failed at write time sits in the table
    with delivered_at=None. After the replay cron runs (with a working
    Redis client), the event should be re-published and marked delivered."""
    from app.jobs.notifications import replay_undelivered_bot_events

    event = BotEvent(type="payment_succeeded", telegram_id=42, payload={})
    db.session.add(event)
    db.session.flush()
    event.created_at = _aged(120)
    db.session.commit()

    fake_redis = MagicMock()
    with patch("app.jobs.notifications._get_redis", return_value=fake_redis):
        replay_undelivered_bot_events()

    fake_redis.publish.assert_called_once()
    refreshed = db.session.get(BotEvent, event.id)
    assert refreshed.delivered_at is not None


def test_replay_skips_recent_events_to_avoid_racing_publish(app, db):
    """Don't replay events younger than the grace window — the original
    publish() may still be in flight or its commit hasn't landed yet."""
    from app.jobs.notifications import replay_undelivered_bot_events

    db.session.add(BotEvent(type="payment_succeeded", telegram_id=42, payload={}))
    db.session.commit()

    fake_redis = MagicMock()
    with patch("app.jobs.notifications._get_redis", return_value=fake_redis):
        replay_undelivered_bot_events()

    fake_redis.publish.assert_not_called()


def test_replay_skips_already_delivered(app, db):
    """delivered_at IS NOT NULL → not a replay candidate."""
    from app.jobs.notifications import replay_undelivered_bot_events

    e = BotEvent(type="t", telegram_id=42, payload={})
    db.session.add(e)
    db.session.flush()
    e.created_at = _aged(120)
    e.delivered_at = dt.datetime.utcnow()
    db.session.commit()

    fake_redis = MagicMock()
    with patch("app.jobs.notifications._get_redis", return_value=fake_redis):
        replay_undelivered_bot_events()

    fake_redis.publish.assert_not_called()


def test_replay_keeps_delivered_at_null_when_publish_fails(app, db):
    """A failed replay leaves the event in the queue for the next run."""
    from app.jobs.notifications import replay_undelivered_bot_events

    e = BotEvent(type="payment_succeeded", telegram_id=42, payload={})
    db.session.add(e)
    db.session.flush()
    e.created_at = _aged(120)
    db.session.commit()

    fake_redis = MagicMock()
    fake_redis.publish.side_effect = ConnectionError("redis down")
    with patch("app.jobs.notifications._get_redis", return_value=fake_redis):
        replay_undelivered_bot_events()

    refreshed = db.session.get(BotEvent, e.id)
    assert refreshed.delivered_at is None


def test_replay_noop_when_redis_unconfigured(app, db):
    """If _get_redis returns None we shouldn't crash — just leave the queue."""
    from app.jobs.notifications import replay_undelivered_bot_events

    e = BotEvent(type="t", telegram_id=42, payload={})
    db.session.add(e)
    db.session.flush()
    e.created_at = _aged(120)
    db.session.commit()

    with patch("app.jobs.notifications._get_redis", return_value=None):
        replay_undelivered_bot_events()  # must not raise

    refreshed = db.session.get(BotEvent, e.id)
    assert refreshed.delivered_at is None
