import logging
import time

import gevent
import gevent.pool

from panel_core.extensions import db, new_shared_redis_subscriber
from panel_core.models import LinkedPanel
from panel_core.services.panel_proxy import (
    REFRESH_CHANNEL,
    FederationClient,
    store_panel_offline,
    store_panel_snapshot,
)

logger = logging.getLogger(__name__)

_RECONNECT_DELAY = 5


def _poll_one(panel_id, url, token):
    try:
        client = FederationClient(url, token)
        data = client.snapshot()
        ts = data.get("timestamp")
        if ts and ts < 100_000_000_000:
            ts *= 1000
        ts = ts or int(time.time() * 1000)
        store_panel_snapshot(panel_id, data, ts)
        return panel_id, "online", None, ts
    except Exception as exc:
        store_panel_offline(panel_id)
        return panel_id, "offline", str(exc)[:500], None


def _record(results):
    dirty = False
    for result in results:
        if result is None:
            continue
        panel_id, status, error, ts = result
        panel = db.session.get(LinkedPanel, panel_id)
        if panel is None:
            continue
        if panel.status == status and (panel.last_error or None) == (error or None):
            continue
        logger.info("panel %s: %s → %s", panel.name, panel.status, status)
        panel.status = status
        if status == "online":
            panel.last_poll = ts
            panel.last_error = None
        else:
            panel.last_error = error
        dirty = True

    if dirty:
        db.session.commit()


def poll_linked_panels():
    panels = LinkedPanel.query.filter_by(enable=True).all()
    if not panels:
        return

    pool = gevent.pool.Pool(size=10)
    jobs = [pool.spawn(_poll_one, p.id, p.url, p.federation_token) for p in panels]
    pool.join()

    _record([job.value for job in jobs])


def poll_panel_now(panel_id):
    panel = db.session.get(LinkedPanel, panel_id)
    if panel is None or not panel.enable:
        return
    _record([_poll_one(panel.id, panel.url, panel.federation_token)])


def run_refresh_listener(app):
    while True:
        client = new_shared_redis_subscriber()
        if client is None:
            app.logger.warning(
                "no shared Redis configured — nothing listens on %s, so a panel is only refreshed by the "
                "10s poll and an admin action takes up to that long to show up",
                REFRESH_CHANNEL,
            )
            return
        try:
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(REFRESH_CHANNEL)
            app.logger.info("subscribed to %s", REFRESH_CHANNEL)
            for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    panel_id = int(message.get("data"))
                except (TypeError, ValueError):
                    continue
                with app.app_context():
                    try:
                        poll_panel_now(panel_id)
                    except Exception:
                        db.session.rollback()
                        logger.warning("out-of-band refresh failed for panel %s", panel_id, exc_info=True)
        except Exception as exc:
            logger.info("%s listener dropped (%s); reconnecting in %ds", REFRESH_CHANNEL, exc, _RECONNECT_DELAY)
            gevent.sleep(_RECONNECT_DELAY)
