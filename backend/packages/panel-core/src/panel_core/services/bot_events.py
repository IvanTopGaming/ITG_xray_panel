import datetime as _dt
import json
import logging
import os
from typing import Optional

from panel_core.extensions import db
from panel_core.models import BotEvent

logger = logging.getLogger(__name__)
_REDIS_CHANNEL = "bot:events"


_BUS_URI_ENV = "BOT_EVENTS_REDIS_URI"
_FALLBACK_URI_ENV = "RATELIMIT_STORAGE_URI"
_REDIS_SCHEMES = ("redis://", "rediss://")


def event_bus_uri() -> str:

    dedicated = (os.getenv(_BUS_URI_ENV, "") or "").strip()
    if dedicated:
        return dedicated
    return (os.getenv(_FALLBACK_URI_ENV, "") or "").strip()


def _get_redis():

    try:
        import redis as redis_lib
    except ImportError:
        return None

    uri = event_bus_uri()
    if not uri.startswith(_REDIS_SCHEMES):
        if uri:
            logger.warning("bot events bus URI %r is not a redis URI; events will not be published", uri)
        return None
    try:
        return redis_lib.Redis.from_url(uri, socket_connect_timeout=1)
    except Exception as exc:
        logger.info("Failed to construct redis client: %s", exc)
        return None


def publish(event_type: str, telegram_id: Optional[int], payload: dict) -> None:

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
