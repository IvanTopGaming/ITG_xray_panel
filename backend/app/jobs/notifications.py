"""Backend-driven expiry + traffic notifications, BotEvent replay + cleanup."""

from __future__ import annotations

import datetime as dt
import logging
import time

from app.extensions import db
from app.models import (
    BotEvent,
    Client,
    NotificationLog,
    Tariff,
    TelegramUser,
    UserTariffAccess,
)
from app.services import bot_events
from app.services.bot_events import _get_redis  # noqa: F401 — re-exported for replay job tests to patch

logger = logging.getLogger(__name__)

_WINDOW_MS = 15 * 60 * 1000
_BUCKETS = (
    ("expiry_3d", 3 * 86400 * 1000),
    ("expiry_1d", 1 * 86400 * 1000),
    ("expiry_1h", 3600 * 1000),
    ("expired", 0),
)

# Order matters — pick the highest bucket the client crosses (97% → traffic_95, not _80).
_TRAFFIC_BUCKETS = (
    ("traffic_exhausted", 1.0),
    ("traffic_95", 0.95),
    ("traffic_80", 0.80),
)


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
    """True if a Renew button would reach a working checkout. Mirrors `_ensure_tariff_available`."""
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


def send_expiry_notifications() -> None:
    now_ms = _now_ms()
    lang_cache: dict[int, str] = {}
    renewable_cache: dict = {}

    for kind, offset_ms in _BUCKETS:
        window_lo = now_ms + offset_ms - _WINDOW_MS
        window_hi = now_ms + offset_ms

        clients = (
            Client.query.filter(Client.telegram_id.isnot(None))
            .filter(Client.enable.is_(True))
            .filter(Client.expiry_time > 0)
            .filter(Client.expiry_time >= window_lo)
            .filter(Client.expiry_time <= window_hi)
            .limit(500)
            .all()
        )
        for c in clients:
            already = NotificationLog.query.filter_by(telegram_id=c.telegram_id, client_id=c.id, kind=kind).first()
            if already is not None:
                continue
            db.session.add(
                NotificationLog(
                    telegram_id=c.telegram_id,
                    client_id=c.id,
                    kind=kind,
                )
            )
            db.session.commit()
            bot_events.publish(
                "expiry_notification",
                c.telegram_id,
                {
                    "kind": kind,
                    "client_id": c.id,
                    "email": c.email,
                    "expiry_time_ms": c.expiry_time,
                    "tariff_id": c.tariff_id,  # None for pre-billing legacy clients
                    "renewable": _is_renewable(c.tariff_id, c.telegram_id, renewable_cache),
                    "lang": _lookup_lang(c.telegram_id, lang_cache),
                },
            )


def send_traffic_notifications() -> None:
    """Warn at 80/95/100% of per-inbound limit."""
    lang_cache: dict[int, str] = {}
    renewable_cache: dict = {}

    clients = (
        Client.query.filter(Client.telegram_id.isnot(None))
        .filter(Client.enable.is_(True))
        .filter(Client.limit_bytes > 0)
        .limit(2000)
        .all()
    )
    for c in clients:
        used_bytes = (c.up or 0) + (c.down or 0)
        limit_bytes = c.limit_bytes
        if not limit_bytes:
            continue
        pct = used_bytes / limit_bytes
        limit_kind = "per_inbound"

        kind = next((k for k, threshold in _TRAFFIC_BUCKETS if pct >= threshold), None)
        if kind is None:
            continue

        already = NotificationLog.query.filter_by(telegram_id=c.telegram_id, client_id=c.id, kind=kind).first()
        if already is not None:
            continue
        db.session.add(NotificationLog(telegram_id=c.telegram_id, client_id=c.id, kind=kind))
        db.session.commit()
        bot_events.publish(
            "traffic_notification",
            c.telegram_id,
            {
                "kind": kind,
                "client_id": c.id,
                "email": c.email,
                "used_bytes": used_bytes,
                "limit_bytes": limit_bytes,
                "limit_kind": limit_kind,
                "pct": round(pct, 4),
                "tariff_id": c.tariff_id,
                "renewable": _is_renewable(c.tariff_id, c.telegram_id, renewable_cache),
                "lang": _lookup_lang(c.telegram_id, lang_cache),
            },
        )


def replay_undelivered_bot_events() -> None:
    """Re-publish events whose original Redis publish failed. 30s grace avoids racing in-flight publishes."""
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
    """Prune delivered events > 7d, undelivered > 30d."""
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
