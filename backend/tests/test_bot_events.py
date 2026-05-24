"""Tests for the bot_events publisher."""

from unittest.mock import patch, MagicMock

from app.models import BotEvent
from app.services.bot_events import publish


def test_publish_inserts_bot_event_row(app, db):
    publish("texts_changed", telegram_id=None, payload={"lang": "ru"})
    rows = BotEvent.query.all()
    assert len(rows) == 1
    assert rows[0].type == "texts_changed"
    assert rows[0].telegram_id is None
    assert rows[0].payload == {"lang": "ru"}
    assert rows[0].delivered_at is None


def test_publish_attempts_redis_publish(app, db):
    with patch("app.services.bot_events._get_redis") as mock_get_redis:
        fake_redis = MagicMock()
        mock_get_redis.return_value = fake_redis
        publish("payment_succeeded", telegram_id=42, payload={"amount": 100})
    fake_redis.publish.assert_called_once()
    channel, message = fake_redis.publish.call_args.args
    assert channel == "bot:events"
    assert "payment_succeeded" in message


def test_publish_swallows_redis_error(app, db):
    """If Redis is unreachable, publish must NOT raise — the BotEvent row
    is the recovery buffer."""
    with patch("app.services.bot_events._get_redis") as mock_get_redis:
        fake_redis = MagicMock()
        fake_redis.publish.side_effect = ConnectionError("redis down")
        mock_get_redis.return_value = fake_redis
        # Must not raise
        publish("texts_changed", telegram_id=None, payload={})
    # Row still inserted
    assert BotEvent.query.count() == 1


def test_publish_when_redis_unavailable_returns_none(app, db):
    """If _get_redis returns None (no redis configured / lib missing),
    the event is still buffered."""
    with patch("app.services.bot_events._get_redis") as mock_get_redis:
        mock_get_redis.return_value = None
        publish("texts_changed", telegram_id=None, payload={})
    assert BotEvent.query.count() == 1


def test_publish_marks_delivered_at_on_successful_redis_publish(app, db):
    """A successful Redis publish should set delivered_at so the replay
    cron does not re-send the same event."""
    with patch("app.services.bot_events._get_redis") as mock_get_redis:
        fake_redis = MagicMock()
        mock_get_redis.return_value = fake_redis
        publish("payment_succeeded", telegram_id=42, payload={})
    row = BotEvent.query.one()
    assert row.delivered_at is not None


def test_publish_leaves_delivered_at_null_when_redis_publish_fails(app, db):
    """A failed Redis publish keeps delivered_at=None so the replay cron
    will pick the event up and try again."""
    with patch("app.services.bot_events._get_redis") as mock_get_redis:
        fake_redis = MagicMock()
        fake_redis.publish.side_effect = ConnectionError("redis down")
        mock_get_redis.return_value = fake_redis
        publish("payment_succeeded", telegram_id=42, payload={})
    row = BotEvent.query.one()
    assert row.delivered_at is None
