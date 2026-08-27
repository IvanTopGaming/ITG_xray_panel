import datetime as dt
import logging
import time

from panel_core.extensions import db
from panel_core.models import BotEvent, SystemSetting
from panel_core.panel_role import ROLE_WORKER, current_role

logger = logging.getLogger(__name__)

SUPERSEDED_SETTING_KEY = "node_superseded_at"
SUPERSEDED_LOCAL_CLOCK_SETTING_KEY = "node_superseded_local_clock_ms"


def _read_int_setting(key: str) -> int:
    row = db.session.get(SystemSetting, key)
    try:
        return int((row.value or "0").strip()) if row else 0
    except ValueError:
        return 0


def _write_setting(key: str, value: str) -> None:
    row = db.session.get(SystemSetting, key)
    if row is None:
        db.session.add(SystemSetting(key=key, value=value))
    else:
        row.value = value


def is_superseded() -> bool:
    if current_role() != ROLE_WORKER:
        return False
    row = db.session.get(SystemSetting, SUPERSEDED_SETTING_KEY)
    return bool(row and (row.value or "").strip() not in ("", "0"))


def superseded_at() -> int:
    return _read_int_setting(SUPERSEDED_SETTING_KEY)


def mark_superseded(at_ms: int) -> None:
    value = str(int(at_ms) if at_ms else int(time.time() * 1000))
    _write_setting(SUPERSEDED_SETTING_KEY, value)
    _write_setting(SUPERSEDED_LOCAL_CLOCK_SETTING_KEY, str(int(time.time() * 1000)))
    db.session.commit()


def clear_superseded() -> None:
    if current_role() != ROLE_WORKER:
        return
    row = db.session.get(SystemSetting, SUPERSEDED_SETTING_KEY)
    if row is None:
        return
    local_clock_ms = _read_int_setting(SUPERSEDED_LOCAL_CLOCK_SETTING_KEY)
    row.value = "0"
    if local_clock_ms:
        since_ms = (local_clock_ms // 1000 - 1) * 1000
        since = dt.datetime.utcfromtimestamp(since_ms / 1000)
        silenced = BotEvent.query.filter(
            BotEvent.delivered_at.is_(None),
            BotEvent.created_at >= since,
        ).update({"delivered_at": dt.datetime.utcnow()}, synchronize_session=False)
        logger.info("clear_superseded: silenced %d bot_event row(s) accumulated since %s", silenced, since)
    db.session.commit()
