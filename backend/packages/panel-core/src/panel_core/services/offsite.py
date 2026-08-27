"""The only sign that the dumps are still leaving the data tier.

The `offsite-backup` container writes three rows into `system_setting` after every successful
`rclone` pass, and this module is their only reader. Nothing else can report the failure: the loop
swallows a non-zero pass on purpose -- a restart loop is not a diagnosis -- so a revoked token, a
full remote or an expired OAuth grant looks exactly like a healthy container from the outside. The
age of the mark is the whole signal.

Staleness is measured in the container's own interval, which is why the script records that too. The
panel has no way to learn how often that box was told to upload; a fixed threshold would cry on a
deployment that uploads daily and stay quiet for hours on one that uploads every ten minutes. Three
intervals is two missed cycles of slack.

Only the shared Postgres ever receives these rows. A node keeps its own SQLite and runs no such
container, so the reading answers `applicable: False` there rather than "never uploaded" -- the same
absence means "not this role" on a node and "the backups are gone" on a master.
"""

import logging
import time

from flask import current_app
from sqlalchemy import text

from panel_core.db_config import is_postgres
from panel_core.extensions import db

logger = logging.getLogger(__name__)

LAST_SUCCESS_KEY = "offsite_backup_last_success_ms"
INTERVAL_KEY = "offsite_backup_interval_seconds"
REMOTE_KEY = "offsite_backup_remote"

STALE_AFTER_INTERVALS = 3
FALLBACK_INTERVAL_SECONDS = 1800


def _as_int(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def read_status():
    if not is_postgres(current_app.config.get("SQLALCHEMY_DATABASE_URI", "")):
        return {"applicable": False}

    try:
        result = db.session.execute(
            text("SELECT key, value FROM system_setting WHERE key IN (:last, :every, :remote)"),
            {"last": LAST_SUCCESS_KEY, "every": INTERVAL_KEY, "remote": REMOTE_KEY},
        )
        rows = {row[0]: row[1] for row in result}
    except Exception as exc:
        logger.debug("offsite: reading the success mark failed: %s", exc)
        db.session.rollback()
        return {"applicable": True, "available": False}

    last = _as_int(rows.get(LAST_SUCCESS_KEY))
    interval = _as_int(rows.get(INTERVAL_KEY))
    threshold = STALE_AFTER_INTERVALS * (interval or FALLBACK_INTERVAL_SECONDS)
    age = None if last is None else max(0, int(time.time()) - last // 1000)

    return {
        "applicable": True,
        "available": True,
        "last_success_at_ms": last,
        "age_seconds": age,
        "interval_seconds": interval,
        "stale_after_seconds": threshold,
        "remote": rows.get(REMOTE_KEY) or None,
        "stale": age is not None and age > threshold,
    }
