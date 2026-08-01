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
LAST_SEEN_KEY = "panel:bot:status:last"

logger = logging.getLogger(__name__)


def record_bot_version(version):
    if not version:
        return
    r = get_shared_redis()
    if r is None:
        return
    payload = json.dumps({"version": str(version), "reported_at": time.time()}).encode()
    try:
        r.setex(STATUS_KEY, DEFAULT_FRESHNESS_S, payload)
        r.set(LAST_SEEN_KEY, payload)
    except Exception as exc:
        logger.debug("bot_status: write to the shared tier failed: %s", exc)


def record_bot_username(username):
    """The panel cannot ask Telegram who the bot is — it holds a token, not a session.

    §109 needs the handle to tell an expired user where to renew, and the bot is the one process
    that already knows it. It rides the same 60-second runtime-config poll the version does, and
    is written only when it changes, so a renamed bot corrects itself within a minute.
    """

    from panel_core.extensions import db
    from panel_core.models import SystemSetting

    handle = str(username or "").strip().lstrip("@")
    if not handle:
        return
    row = SystemSetting.query.filter_by(key="bot_username").first()
    if row is None:
        db.session.add(SystemSetting(key="bot_username", value=handle))
    elif row.value == handle:
        return
    else:
        row.value = handle
    db.session.commit()


def get_bot_status(freshness=DEFAULT_FRESHNESS_S):
    """`state` separates "never reported" from "stopped reporting" -- see role_status._read_one."""

    empty = {"version": None, "reported_at": None, "state": None}
    r = get_shared_redis()
    if r is None:
        return empty
    try:
        fresh = _decode(r.get(STATUS_KEY))
        last = _decode(r.get(LAST_SEEN_KEY))
    except Exception as exc:
        logger.debug("bot_status: read from the shared tier failed: %s", exc)
        return empty
    if fresh is not None and (time.time() - fresh["reported_at"]) <= freshness:
        return {**fresh, "state": "reporting"}
    if last is not None:
        return {**last, "state": "silent"}
    return empty


def _decode(raw):
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        version = str(payload["version"])
        reported_at = float(payload["reported_at"])
    except (ValueError, TypeError, KeyError):
        return None
    if not version:
        return None
    return {"version": version, "reported_at": reported_at}
