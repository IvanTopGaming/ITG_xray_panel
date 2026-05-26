import json
import logging
import time

import gevent.pool

from app.extensions import db, get_redis
from app.models import LinkedPanel
from app.services.panel_proxy import FederationClient

logger = logging.getLogger(__name__)


def poll_linked_panels():
    panels = LinkedPanel.query.filter_by(enable=True).all()
    if not panels:
        return

    pool = gevent.pool.Pool(size=10)

    def _poll_one(panel_id, url, token, name):
        try:
            client = FederationClient(url, token)
            data = client.snapshot()
            r = get_redis()
            if r:
                r.setex(f"panel:{panel_id}:snapshot", 60, json.dumps(data))
                r.setex(f"panel:{panel_id}:status", 120, "online")
            return panel_id, "online", None, data.get("timestamp")
        except Exception as exc:
            return panel_id, "offline", str(exc)[:500], None

    jobs = []
    for p in panels:
        jobs.append(pool.spawn(_poll_one, p.id, p.url, p.federation_token, p.name))

    pool.join()

    for job in jobs:
        panel_id, status, error, ts = job.value
        panel = db.session.get(LinkedPanel, panel_id)
        if panel is None:
            continue
        panel.status = status
        if status == "online":
            panel.last_poll = ts or int(time.time() * 1000)
            panel.last_error = None
        else:
            panel.last_error = error

    db.session.commit()
