import datetime as _dt
import json
import logging
import uuid
from typing import Optional
from sqlalchemy import text

from panel_core.extensions import db, get_shared_redis
from panel_core.models import BotEvent, SystemSetting
from panel_core.panel_role import is_worker
from panel_core.services.supersede import is_superseded

logger = logging.getLogger(__name__)
_REDIS_CHANNEL = "bot:events"
_redis_unavailable_logged = False


def _get_redis():
    global _redis_unavailable_logged
    client = get_shared_redis()
    if client is None:
        if not _redis_unavailable_logged:
            logger.warning("bot event Redis unavailable; events remain in the durable outbox")
            _redis_unavailable_logged = True
    elif _redis_unavailable_logged:
        logger.info("bot event Redis recovered")
        _redis_unavailable_logged = False
    return client


def event_source() -> str:
    key = "node_instance_id" if is_worker() else "bot_event_source"
    row = db.session.get(SystemSetting, key)
    if row is not None and (row.value or "").strip():
        value = row.value.strip()
        return f"node:{value}" if is_worker() else f"shared:{value}"
    value = uuid.uuid4().hex
    db.session.execute(
        text("INSERT INTO system_setting (key, value) VALUES (:key, :value) ON CONFLICT (key) DO NOTHING"),
        {"key": key, "value": value},
    )
    db.session.execute(
        text("UPDATE system_setting SET value = :value WHERE key = :key AND (value IS NULL OR trim(value) = '')"),
        {"key": key, "value": value},
    )
    if row is not None:
        db.session.expire(row)
    value = db.session.get(SystemSetting, key).value
    return f"node:{value}" if is_worker() else f"shared:{value}"


def enqueue(event_type: str, telegram_id: Optional[int], payload: dict) -> BotEvent:
    event = BotEvent(type=event_type, telegram_id=telegram_id, payload=payload, source=event_source())
    db.session.add(event)
    ensure_event_identity(event)
    return event


def ensure_event_identity(event: BotEvent) -> None:
    if not event.source:
        event.source = event_source()
    if event.id is None:
        db.session.flush()
    if event.origin_event_id is None:
        event.origin_event_id = event.id


def publish(event_type: str, telegram_id: Optional[int], payload: dict) -> None:

    event = enqueue(event_type, telegram_id, payload)
    db.session.commit()
    publish_stored(event)


def publish_stored(event: BotEvent) -> None:
    event_type = event.type
    if not event.source or event.origin_event_id is None:
        ensure_event_identity(event)
        db.session.commit()
    if is_superseded():
        logger.info("event %s stored but not published: this installation has been superseded", event_type)
        return

    redis_client = _get_redis()
    if redis_client is None:
        return

    message = json.dumps(
        {
            "id": event.origin_event_id,
            "source": event.source,
            "sender": event_source(),
            "created_at_ms": int(event.created_at.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000),
            "type": event_type,
            "telegram_id": event.telegram_id,
            "payload": event.payload,
        }
    )
    try:
        subscribers = redis_client.publish(_REDIS_CHANNEL, message)
    except Exception as exc:
        logger.warning("Redis publish failed for %s: %s", event_type, exc)
        return
    if subscribers == 0:
        event.delivered_at = _dt.datetime.utcnow()
        db.session.commit()
