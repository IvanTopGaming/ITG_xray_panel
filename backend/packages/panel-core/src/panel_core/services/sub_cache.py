import logging
import os

from panel_core.extensions import LOCAL_REDIS_URI_ENV, get_redis, get_shared_redis, shared_redis_uri
from panel_core.models import Client

logger = logging.getLogger(__name__)

KINDS = ("v2ray", "clash", "singbox")
AGG_KINDS = ("u-v2ray", "u-clash", "u-singbox")
TTL = int(os.getenv("SUB_CACHE_TTL_SECONDS", "60") or "60")
_warned = set()


def _warn_once(scope, exc):
    if scope in _warned:
        return
    _warned.add(scope)
    if scope == "shared":
        logger.info(
            "sub_cache: the shared Redis rejected an invalidation (%s). Expected on a node, whose data-tier "
            "credential is publish-only; the sub host's cache expires on its own within SUB_CACHE_TTL_SECONDS.",
            exc,
        )
    else:
        logger.info("sub_cache: Redis unavailable, falling through (%s)", exc)


def _key(kind, uuid_str):
    return f"sub:{kind}:{uuid_str}"


def _invalidation_targets():

    targets = []
    local = get_redis()
    if local is not None:
        targets.append(("local", local))

    shared = shared_redis_uri()
    if shared and shared != (os.getenv(LOCAL_REDIS_URI_ENV, "") or "").strip():
        client = get_shared_redis()
        if client is not None:
            targets.append(("shared", client))
    return targets


def _delete(keys):

    if not keys:
        return
    for scope, client in _invalidation_targets():
        try:
            client.delete(*keys)
        except Exception as e:
            _warn_once(scope, e)


def get(kind, uuid_str):
    if TTL <= 0:
        return None
    r = get_redis()
    if r is None:
        return None
    try:
        return r.get(_key(kind, uuid_str))
    except Exception as e:
        _warn_once("local", e)
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
        _warn_once("local", e)


def invalidate_user(uuid_str):
    if not uuid_str:
        return
    _delete([_key(k, uuid_str) for k in KINDS])


def invalidate_all_for_inbound(inbound_tag):

    if not inbound_tag:
        return
    try:
        rows = Client.query.filter_by(inbound_tag=inbound_tag).with_entities(Client.id).all()
    except Exception as e:
        _warn_once("local", e)
        return
    if not rows:
        return

    chunk = []
    for (uuid_str,) in rows:
        if not uuid_str:
            continue
        for k in KINDS:
            chunk.append(_key(k, uuid_str))
        if len(chunk) >= 1500:
            _delete(chunk)
            chunk = []
    _delete(chunk)


def invalidate_user_aggregate(telegram_id):

    if not telegram_id:
        return
    from panel_core.models import TelegramUser

    try:
        row = TelegramUser.query.filter_by(telegram_id=telegram_id).with_entities(TelegramUser.sub_token).first()
    except Exception as e:
        _warn_once("local", e)
        return
    token = row[0] if row else None
    if not token:
        return
    _delete([_key(k, token) for k in AGG_KINDS])
