import datetime as _dt
import json
import logging
from typing import Optional

from panel_core.extensions import db, get_shared_redis
from panel_core.models import BotEvent

logger = logging.getLogger(__name__)
_REDIS_CHANNEL = "bot:events"


def _get_redis():

    return get_shared_redis()


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
