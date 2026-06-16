from __future__ import annotations

import datetime as dt
import logging
import time

from app.extensions import db
from app.models import (
    BotEvent,
    NotificationLog,
    Tariff,
    TelegramUser,
    UserTariffAccess,
)
from app.services import bot_events
from app.services.bot_events import _get_redis  # noqa: F401 — re-exported for replay job tests to patch

logger = logging.getLogger(__name__)

_TRAFFIC_BUCKETS = (
    ("traffic_exhausted", 1.0),
    ("traffic_95", 0.95),
    ("traffic_80", 0.80),
)


def evaluate_traffic(client) -> str | None:
    limit_bytes = client.limit_bytes or 0
    if limit_bytes <= 0:
        return None
    pct = ((client.up or 0) + (client.down or 0)) / limit_bytes
    for kind, threshold in _TRAFFIC_BUCKETS:
        if pct >= threshold:
            return kind
    return None


_EXPIRY_THRESHOLDS = (
    ("expired", 0),
    ("expiry_1h", 3600 * 1000),
    ("expiry_1d", 86400 * 1000),
    ("expiry_3d", 3 * 86400 * 1000),
)


def evaluate_expiry(client, now_ms: int) -> str | None:
    if not client.expiry_time or client.expiry_time <= 0:
        return None
    remaining = client.expiry_time - now_ms
    for kind, threshold_ms in _EXPIRY_THRESHOLDS:
        if remaining <= threshold_ms:
            return kind
    return None


def emit_if_new(event_type, kind, client, extra, *, lang_cache=None, renewable_cache=None) -> bool:
    already = NotificationLog.query.filter_by(telegram_id=client.telegram_id, client_id=client.id, kind=kind).first()
    if already is not None:
        return False
    db.session.add(NotificationLog(telegram_id=client.telegram_id, client_id=client.id, kind=kind))
    db.session.commit()
    lang_cache = {} if lang_cache is None else lang_cache
    renewable_cache = {} if renewable_cache is None else renewable_cache
    payload = {
        "kind": kind,
        "client_id": client.id,
        "email": client.email,
        **extra,
        "tariff_id": client.tariff_id,
        "renewable": _is_renewable(client.tariff_id, client.telegram_id, renewable_cache),
        "lang": _lookup_lang(client.telegram_id, lang_cache),
    }
    bot_events.publish(event_type, client.telegram_id, payload)
    return True


def _now_ms() -> int:
    return int(time.time() * 1000)


def _lookup_lang(telegram_id: int, cache: dict[int, str]) -> str:
    if telegram_id in cache:
        return cache[telegram_id]
    row = TelegramUser.query.filter_by(telegram_id=telegram_id).first()
    lang = row.language if row and row.language else "ru"
    cache[telegram_id] = lang
    return lang


def _is_renewable(tariff_id: int | None, telegram_id: int, cache: dict[int, bool]) -> bool:

    if tariff_id is None:
        return False
    key = (tariff_id, telegram_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    tariff = db.session.get(Tariff, tariff_id)
    if tariff is None or not tariff.enabled or tariff.visibility == "archived" or tariff.is_trial:
        result = False
    elif tariff.visibility == "private":
        grant = UserTariffAccess.query.filter_by(telegram_id=telegram_id, tariff_id=tariff_id).first()
        result = grant is not None
    else:
        result = True
    cache[key] = result
    return result


def replay_undelivered_bot_events() -> None:

    import json as _json

    redis_client = _get_redis()
    if redis_client is None:
        return

    cutoff = dt.datetime.utcnow() - dt.timedelta(seconds=30)
    undelivered = (
        BotEvent.query.filter(
            BotEvent.delivered_at.is_(None),
            BotEvent.created_at < cutoff,
        )
        .order_by(BotEvent.id.asc())
        .limit(200)
        .all()
    )
    if not undelivered:
        return

    now = dt.datetime.utcnow()
    for event in undelivered:
        message = _json.dumps(
            {
                "id": event.id,
                "type": event.type,
                "telegram_id": event.telegram_id,
                "payload": event.payload,
            }
        )
        try:
            redis_client.publish("bot:events", message)
        except Exception as exc:
            logger.info("replay: publish failed for event=%s: %s", event.id, exc)
            continue
        event.delivered_at = now
    db.session.commit()


def cleanup_bot_events() -> None:

    now = dt.datetime.utcnow()
    BotEvent.query.filter(
        BotEvent.delivered_at.isnot(None),
        BotEvent.created_at < now - dt.timedelta(days=7),
    ).delete(synchronize_session=False)
    BotEvent.query.filter(
        BotEvent.delivered_at.is_(None),
        BotEvent.created_at < now - dt.timedelta(days=30),
    ).delete(synchronize_session=False)
    db.session.commit()
