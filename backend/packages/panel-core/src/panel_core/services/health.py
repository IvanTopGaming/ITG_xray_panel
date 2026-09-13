"""§10.8: the things an admin currently finds out from a user complaint.

Metrics went away in phase 6, the logs are json-file rotations on five separate machines that are
collected nowhere, and `/healthz` and `/readyz` answer "this process is up" and "its database
answers". Everything between those and a real outage is invisible:

* **undelivered `bot_event` rows** -- the only indicator that the bus is broken. A `PUBLISH` with no
  subscriber still succeeds, so the recovery buffer protects against Redis being down and not
  against the bot being down (deliberately);
* **payments stuck in `processing`** -- money taken, access not granted, and no UI filter that shows
  them (§23);
* **the data tier itself** -- nobody knows it is unwell until a request fails;
* **the off-site copy** -- the dumps leaving the machine that holds them. The container that copies
  them keeps its loop alive through every failure on purpose, so a revoked token or a full remote
  looks healthy from the outside and the age of its success mark is the only signal there is.

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
import datetime as dt

from sqlalchemy import text

from panel_core.extensions import db, get_shared_redis
from panel_core.services.offsite import read_status as read_offsite_status
from panel_core.services.job_status import read_job_status

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
        pending_fulfillment = int(
            _scalar(
                "SELECT COUNT(*) FROM payment WHERE provider_status = 'succeeded' AND fulfillment_status <> 'succeeded'"
            )
        )
        pending_refunds = int(
            _scalar(
                "SELECT COUNT(*) FROM payment WHERE refund_status IN ('pending', 'processing', 'retry', 'review', 'failed')"
            )
        )
        review = int(
            _scalar(
                "SELECT COUNT(*) FROM payment WHERE checkout_status = 'review' OR fulfillment_status = 'review' OR refund_status = 'review'"
            )
        )
        return {
            "available": True,
            "processing": processing,
            "pending_over_a_day": pending,
            "pending_fulfillment": pending_fulfillment,
            "pending_refunds": pending_refunds,
            "review": review,
        }
    except Exception as exc:
        logger.debug("health: stuck payment count failed: %s", exc)
        db.session.rollback()
        return {"available": False}


def _event_delivery():
    from panel_core.models import BotDelivery

    try:
        counts = dict(
            db.session.query(BotDelivery.state, db.func.count(BotDelivery.id)).group_by(BotDelivery.state).all()
        )
        oldest = (
            db.session.query(db.func.min(BotDelivery.created_at))
            .filter(BotDelivery.state.in_(("pending", "leased")))
            .scalar()
        )
        oldest_ms = int(oldest.replace(tzinfo=dt.timezone.utc).timestamp() * 1000) if oldest else None
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        attention = bool(
            counts.get("review") or counts.get("permanent") or (oldest_ms is not None and now_ms - oldest_ms > 300000)
        )
        return {
            "available": True,
            "pending": counts.get("pending", 0),
            "leased": counts.get("leased", 0),
            "review": counts.get("review", 0),
            "permanent": counts.get("permanent", 0),
            "oldest_pending_ms": oldest_ms,
            "needs_attention": attention,
        }
    except Exception as exc:
        logger.warning("health: event inbox reading failed: %s", type(exc).__name__)
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
            logger.debug("health: shared Redis probe failed: %s", exc)
    else:
        shared_redis = "not configured"

    return {"database": database, "shared_redis": shared_redis}


def _offsite_backup():
    try:
        return read_offsite_status()
    except Exception as exc:
        logger.debug("health: offsite reading failed: %s", exc)
        db.session.rollback()
        return {"applicable": True, "available": False}


def collect():
    return {
        "jobs": read_job_status(),
        "undelivered_events": _undelivered_events(),
        "event_delivery": _event_delivery(),
        "stuck_payments": _stuck_payments(),
        "data_tier": _data_tier(),
        "offsite_backup": _offsite_backup(),
    }
