"""Redis-backed cache for the subscription endpoint.

Caches the rendered response body keyed by (ua_kind, uuid). Falls through silently
when Redis is unreachable or RATELIMIT_STORAGE_URI does not point at Redis.
"""

import logging
import os

from app.extensions import get_redis
from app.models import Client

logger = logging.getLogger(__name__)

KINDS = ("v2ray", "clash", "singbox")
AGG_KINDS = ("u-v2ray", "u-clash", "u-singbox")
TTL = int(os.getenv("SUB_CACHE_TTL_SECONDS", "60") or "60")
_warned = False


def _warn_once(exc):
    global _warned
    if _warned:
        return
    _warned = True
    logger.info("sub_cache: Redis unavailable, falling through (%s)", exc)


def _key(kind, uuid_str):
    return f"sub:{kind}:{uuid_str}"


def get(kind, uuid_str):
    if TTL <= 0:
        return None
    r = get_redis()
    if r is None:
        return None
    try:
        return r.get(_key(kind, uuid_str))
    except Exception as e:
        _warn_once(e)
        return None


def set(kind, uuid_str, value):
    if TTL <= 0 or value is None:
        return
    r = get_redis()
    if r is None:
        return
    try:
        if isinstance(value, str):
            value = value.encode("utf-8")
        r.setex(_key(kind, uuid_str), TTL, value)
    except Exception as e:
        _warn_once(e)


def invalidate_user(uuid_str):
    if not uuid_str:
        return
    r = get_redis()
    if r is None:
        return
    try:
        r.delete(*[_key(k, uuid_str) for k in KINDS])
    except Exception as e:
        _warn_once(e)


def invalidate_all_for_inbound(inbound_tag):
    """Drop every cached subscription belonging to clients of this inbound."""
    if not inbound_tag:
        return
    r = get_redis()
    if r is None:
        return
    try:
        rows = Client.query.filter_by(inbound_tag=inbound_tag).with_entities(Client.id).all()
        if not rows:
            return
        chunk = []
        for (uuid_str,) in rows:
            if not uuid_str:
                continue
            for k in KINDS:
                chunk.append(_key(k, uuid_str))
            if len(chunk) >= 1500:  # ~500 users × 3 kinds
                r.delete(*chunk)
                chunk = []
        if chunk:
            r.delete(*chunk)
    except Exception as e:
        _warn_once(e)


def invalidate_user_aggregate(telegram_id):
    """Drop cached aggregated subscription for a telegram user (by their sub_token)."""
    if not telegram_id:
        return
    from app.models import TelegramUser

    r = get_redis()
    if r is None:
        return
    try:
        row = TelegramUser.query.filter_by(telegram_id=telegram_id).with_entities(TelegramUser.sub_token).first()
        token = row[0] if row else None
        if not token:
            return
        r.delete(*[_key(k, token) for k in AGG_KINDS])
    except Exception as e:
        _warn_once(e)
