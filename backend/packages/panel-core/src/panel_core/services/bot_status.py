"""The bot's reported version, written by bot-api and read by the master.

The bot stamps `X-Bot-Version` on every `GET /bot/runtime-config` (once a minute); that endpoint
lives on **bot-api**. The only reader is `GET /api/system/version`, which ships from
`panel-adminapi` and therefore runs on the **master** -- a different image on a different machine.

Until wave 5b this was a plain dict at module level, so the writer filled a variable in one
container and the reader looked at an empty one in another: System -> About never showed a bot row
at all, whether the bot was alive or not. The same class as the update-check cache in §35, which
the cron split had already broken and which was fixed the same way.

There is deliberately **no in-process fallback**. A dict would only ever be right when writer and
reader are the same process, which no supported topology produces any more, and its only other
effect would be to let an in-process test pass while the split deployment stayed broken.
"""

import json
import logging
import time

from panel_core.extensions import get_shared_redis

DEFAULT_FRESHNESS_S = 180
STATUS_KEY = "panel:bot:status"

logger = logging.getLogger(__name__)


def record_bot_version(version):
    if not version:
        return
    r = get_shared_redis()
    if r is None:
        return
    payload = {"version": str(version), "reported_at": time.time()}
    try:
        r.setex(STATUS_KEY, DEFAULT_FRESHNESS_S, json.dumps(payload).encode())
    except Exception as exc:
        logger.debug("bot_status: write to the shared tier failed: %s", exc)


def get_bot_status(freshness=DEFAULT_FRESHNESS_S):
    r = get_shared_redis()
    if r is None:
        return {"version": None, "reported_at": None}
    try:
        raw = r.get(STATUS_KEY)
    except Exception as exc:
        logger.debug("bot_status: read from the shared tier failed: %s", exc)
        return {"version": None, "reported_at": None}
    if raw is None:
        return {"version": None, "reported_at": None}
    try:
        payload = json.loads(raw)
        version = payload["version"]
        reported_at = float(payload["reported_at"])
    except (ValueError, TypeError, KeyError):
        return {"version": None, "reported_at": None}
    if not version or (time.time() - reported_at) > freshness:
        return {"version": None, "reported_at": None}
    return {"version": str(version), "reported_at": reported_at}
