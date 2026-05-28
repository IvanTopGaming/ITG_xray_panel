"""Backend → bot push events. Dual-writes to Redis pubsub + bot_event table (replay buffer)."""

import datetime as _dt
import json
import logging
import os
from typing import Optional

from app.extensions import db
from app.models import BotEvent

logger = logging.getLogger(__name__)
_REDIS_CHANNEL = "bot:events"


def _get_redis():
    # Returns None if redis-py is missing or RATELIMIT_STORAGE_URI isn't a redis:// URI.
    try:
        import redis as redis_lib
    except ImportError:
        return None

    uri = (os.getenv("RATELIMIT_STORAGE_URI", "") or "").strip()
    if not uri.startswith("redis://"):
        return None
    try:
        return redis_lib.Redis.from_url(uri, socket_connect_timeout=1)
    except Exception as exc:
        logger.info("Failed to construct redis client: %s", exc)
        return None


def publish(event_type: str, telegram_id: Optional[int], payload: dict) -> None:
    """Buffer a BotEvent row first, then best-effort publish. Never raises."""
    event = BotEvent(type=event_type, telegram_id=telegram_id, payload=payload)
    db.session.add(event)
    db.session.commit()

    redis_client = _get_redis()
    if redis_client is None:
        return

    message = json.dumps(
        {
            "id": event.id,
            "type": event_type,
            "telegram_id": telegram_id,
            "payload": payload,
        }
    )
    try:
        redis_client.publish(_REDIS_CHANNEL, message)
    except Exception as exc:
        logger.warning("Redis publish failed for %s: %s", event_type, exc)
        return
    event.delivered_at = _dt.datetime.utcnow()
    db.session.commit()
