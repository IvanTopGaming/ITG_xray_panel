"""§10.8: the things an admin currently finds out from a user complaint.

Metrics went away in phase 6, the logs are json-file rotations on five separate machines that are
collected nowhere, and `/healthz` and `/readyz` answer "this process is up" and "its database
answers". Everything between those and a real outage is invisible:

* **undelivered `bot_event` rows** -- the only indicator that the bus is broken. A `PUBLISH` with no
  subscriber still succeeds, so the recovery buffer protects against Redis being down and not
  against the bot being down (deliberately);
* **payments stuck in `processing`** -- money taken, access not granted, and no UI filter that shows
  them (§23);
* **the data tier itself** -- nobody knows it is unwell until a request fails.

The certificate used to be the first entry on that list, and the strongest: four-plus hosts, one
hand-issued pair each, nothing renewing them, and no date shown anywhere. Caddy issues and renews
them itself now, and keeps the result in its own storage rather than the ./certs directory this
module used to read -- so the only reading left to take would be "not mounted", forever, on a host
that is perfectly healthy. It was removed rather than left lying.

Every reading is per-panel and says so: this reports what *this* host can see. The counts come from
whichever database this role holds, which is the shared Postgres on the master and its own SQLite on
a node -- that is the honest number in both cases, not an approximation of a fleet-wide one.

Nothing here may raise. A health card that 500s because one reading could not be taken is worse than
one that says that reading is unavailable.
"""

import logging

from sqlalchemy import text

from panel_core.extensions import db, get_shared_redis, redis_answered

logger = logging.getLogger(__name__)

STUCK_PENDING_HOURS = 24


def _scalar(sql, **params):
    return db.session.execute(text(sql), params).scalar() or 0


def _undelivered_events():
    try:
        return {
            "count": int(_scalar("SELECT COUNT(*) FROM bot_event WHERE delivered_at IS NULL")),
            "available": True,
        }
    except Exception as exc:
        logger.debug("health: undelivered event count failed: %s", exc)
        db.session.rollback()
        return {"available": False}


def _stuck_payments():
    try:
        processing = int(_scalar("SELECT COUNT(*) FROM payment WHERE status = 'processing'"))
        pending = int(
            _scalar(
                "SELECT COUNT(*) FROM payment WHERE status = 'pending' AND created_at < :cutoff",
                cutoff=_hours_ago(STUCK_PENDING_HOURS),
            )
        )
        return {"available": True, "processing": processing, "pending_over_a_day": pending}
    except Exception as exc:
        logger.debug("health: stuck payment count failed: %s", exc)
        db.session.rollback()
        return {"available": False}


def _hours_ago(hours):
    import datetime as dt

    return dt.datetime.utcnow() - dt.timedelta(hours=hours)


def _data_tier():
    database = "down"
    try:
        db.session.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:
        logger.debug("health: database probe failed: %s", exc)
        db.session.rollback()

    shared_redis = "down"
    client = get_shared_redis()
    if client is not None:
        try:
            client.ping()
            shared_redis = "ok"
        except Exception as exc:
            if redis_answered(exc):
                shared_redis = "ok"
            else:
                logger.debug("health: shared Redis probe failed: %s", exc)
    else:
        shared_redis = "not configured"

    return {"database": database, "shared_redis": shared_redis}


def collect():
    return {
        "undelivered_events": _undelivered_events(),
        "stuck_payments": _stuck_payments(),
        "data_tier": _data_tier(),
    }
