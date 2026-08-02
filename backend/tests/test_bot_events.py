from unittest.mock import patch, MagicMock

from panel_core.models import BotEvent
from panel_core.services.bot_events import publish


def test_publish_inserts_bot_event_row(app, db):
    publish("texts_changed", telegram_id=None, payload={"lang": "ru"})
    rows = BotEvent.query.all()
    assert len(rows) == 1
    assert rows[0].type == "texts_changed"
    assert rows[0].telegram_id is None
    assert rows[0].payload == {"lang": "ru"}
    assert rows[0].delivered_at is None


def test_publish_attempts_redis_publish(app, db):
    with patch("panel_core.services.bot_events._get_redis") as mock_get_redis:
        fake_redis = MagicMock()
        mock_get_redis.return_value = fake_redis
        publish("payment_succeeded", telegram_id=42, payload={"amount": 100})
    fake_redis.publish.assert_called_once()
    channel, message = fake_redis.publish.call_args.args
    assert channel == "bot:events"
    assert "payment_succeeded" in message


def test_publish_swallows_redis_error(app, db):

    with patch("panel_core.services.bot_events._get_redis") as mock_get_redis:
        fake_redis = MagicMock()
        fake_redis.publish.side_effect = ConnectionError("redis down")
        mock_get_redis.return_value = fake_redis

        publish("texts_changed", telegram_id=None, payload={})

    assert BotEvent.query.count() == 1


def test_publish_when_redis_unavailable_returns_none(app, db):

    with patch("panel_core.services.bot_events._get_redis") as mock_get_redis:
        mock_get_redis.return_value = None
        publish("texts_changed", telegram_id=None, payload={})
    assert BotEvent.query.count() == 1


def test_publish_marks_delivered_at_on_successful_redis_publish(app, db):

    with patch("panel_core.services.bot_events._get_redis") as mock_get_redis:
        fake_redis = MagicMock()
        mock_get_redis.return_value = fake_redis
        publish("payment_succeeded", telegram_id=42, payload={})
    row = BotEvent.query.one()
    assert row.delivered_at is not None


def test_publish_leaves_delivered_at_null_when_redis_publish_fails(app, db):

    with patch("panel_core.services.bot_events._get_redis") as mock_get_redis:
        fake_redis = MagicMock()
        fake_redis.publish.side_effect = ConnectionError("redis down")
        mock_get_redis.return_value = fake_redis
        publish("payment_succeeded", telegram_id=42, payload={})
    row = BotEvent.query.one()
    assert row.delivered_at is None


def test_the_bus_reads_the_shared_uri(monkeypatch):
    from panel_core.extensions import shared_redis_uri

    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "redis://local:6379/0")
    monkeypatch.setenv("SHARED_REDIS_URI", "redis://data-tier:6379/0")
    assert shared_redis_uri() == "redis://data-tier:6379/0"


def test_the_bus_never_falls_back_to_the_local_redis(monkeypatch):

    from panel_core.extensions import shared_redis_uri

    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "redis://local:6379/0")
    monkeypatch.delenv("SHARED_REDIS_URI", raising=False)
    assert shared_redis_uri() == "", (
        "the shared-tier URI fell back to the local Redis. That fallback is exactly how a master or a node "
        "published every bot event into a Redis nobody subscribes to: PUBLISH with no subscriber still "
        "returns success, so the bot_event row was stamped delivered_at and the replay cron never retried "
        "it. The event was lost for good, silently. An unset variable must yield nothing, and the compose "
        "files demand it with :? so the stack refuses to start instead."
    )


def test_get_redis_accepts_rediss_scheme(monkeypatch):
    from panel_core.services import bot_events

    monkeypatch.setenv("SHARED_REDIS_URI", "rediss://data-tier:6379/0")
    with patch("redis.Redis.from_url") as from_url:
        from_url.return_value = MagicMock()
        client = bot_events._get_redis()
    assert client is not None
    assert from_url.call_args.args[0] == "rediss://data-tier:6379/0"


def test_get_redis_rejects_non_redis_scheme(monkeypatch):
    from panel_core.services import bot_events

    monkeypatch.setenv("SHARED_REDIS_URI", "memory://")
    assert bot_events._get_redis() is None
