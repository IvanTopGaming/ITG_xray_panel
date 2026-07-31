"""§10.8: the four things an admin currently finds out from a user complaint.

Metrics went away in phase 6, the logs are json-file rotations on five separate machines that are
collected nowhere, and `/healthz` and `/readyz` answer "this process is up" and "its database
answers". Everything between those and a real outage is invisible:

* **the certificate** -- four-plus hosts, one manually renewed pair each, no ACME, no cron, and no
  date shown anywhere. By probability of actually firing this outranks every defect in §7, and a
  missed host disappears completely, masquerade and all;
* **undelivered `bot_event` rows** -- the only indicator that the bus is broken. A `PUBLISH` with no
  subscriber still succeeds, so the recovery buffer protects against Redis being down and not
  against the bot being down (deliberately);
* **payments stuck in `processing`** -- money taken, access not granted, and no UI filter that shows
  them (§23);
* **the data tier itself** -- nobody knows it is unwell until a request fails.

Every reading is per-panel and says so: this reports what *this* host can see. The counts come from
whichever database this role holds, which is the shared Postgres on the master and its own SQLite on
a node -- that is the honest number in both cases, not an approximation of a fleet-wide one.

Nothing here may raise. A health card that 500s because it could not read a certificate is worse
than one that says it could not read the certificate.
"""

import logging
import os

from sqlalchemy import text

from panel_core.extensions import db, get_shared_redis

logger = logging.getLogger(__name__)

CERT_PATH = "/root/cert/fullchain.pem"

STUCK_PENDING_HOURS = 24


def _certificate():
    if not os.path.exists(CERT_PATH):
        return {"available": False, "reason": "not mounted"}
    try:
        from cryptography import x509

        with open(CERT_PATH, "rb") as handle:
            cert = x509.load_pem_x509_certificate(handle.read())
        not_after = cert.not_valid_after_utc
        names = []
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            names = [str(v) for v in san.value.get_values_for_type(x509.DNSName)]
        except x509.ExtensionNotFound:
            names = []
        return {
            "available": True,
            "not_after_ms": int(not_after.timestamp() * 1000),
            "domains": names,
        }
    except Exception as exc:
        logger.debug("health: could not read the certificate: %s", exc)
        return {"available": False, "reason": "unreadable"}


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
            logger.debug("health: shared Redis probe failed: %s", exc)
    else:
        shared_redis = "not configured"

    return {"database": database, "shared_redis": shared_redis}


def collect():
    return {
        "certificate": _certificate(),
        "undelivered_events": _undelivered_events(),
        "stuck_payments": _stuck_payments(),
        "data_tier": _data_tier(),
    }
