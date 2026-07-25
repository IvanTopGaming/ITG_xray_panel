from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from panel_core.extensions import db
from panel_core.models import (
    NotificationClaim,
    NotificationLog,
    Tariff,
    TelegramUser,
    UserTariffAccess,
)
from panel_core.services import bot_events

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


def claim_notification(*, telegram_id: int, kind: str, tariff_id: int | None, scope: str) -> dict:

    normalized_tariff_id = int(tariff_id or 0)
    normalized_scope = scope or ""

    claimed = True
    db.session.add(
        NotificationClaim(
            telegram_id=telegram_id,
            tariff_id=normalized_tariff_id,
            scope=normalized_scope,
            kind=kind,
        )
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        claimed = False

    return {
        "claimed": claimed,
        "lang": _lookup_lang(telegram_id, {}),
        "renewable": _is_renewable(tariff_id, telegram_id, {}) if tariff_id else False,
    }


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
