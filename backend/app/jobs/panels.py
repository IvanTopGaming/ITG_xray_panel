import json
import logging
import time
from datetime import datetime

import gevent.pool

from app.extensions import db, get_redis
from app.models import LinkedPanel
from app.services.panel_proxy import FederationClient
from app.services.stats import _ten_min_bucket, _upsert_node_snapshot

logger = logging.getLogger(__name__)


def _diff_snapshots(prev, curr):
    if not prev:
        return []
    prev_inb = {}
    prev_cli = {}
    for ib in prev.get("inbounds", []) or []:
        tag = ib.get("tag", "")
        prev_inb[tag] = (ib.get("up", 0) or 0, ib.get("down", 0) or 0)
        for c in ib.get("clients", []) or []:
            prev_cli[(tag, c.get("email", ""))] = (c.get("up", 0) or 0, c.get("down", 0) or 0)
    out = []
    for ib in curr.get("inbounds", []) or []:
        tag = ib.get("tag", "")
        pu, pd = prev_inb.get(tag, (0, 0))
        du = max(0, (ib.get("up", 0) or 0) - pu)
        dd = max(0, (ib.get("down", 0) or 0) - pd)
        if du or dd:
            out.append(("inbound", tag, "", du, dd))
        for c in ib.get("clients", []) or []:
            email = c.get("email", "")
            pu, pd = prev_cli.get((tag, email), (0, 0))
            du = max(0, (c.get("up", 0) or 0) - pu)
            dd = max(0, (c.get("down", 0) or 0) - pd)
            if du or dd:
                out.append(("user", email, tag, du, dd))
    return out


def poll_linked_panels():
    panels = LinkedPanel.query.filter_by(enable=True).all()
    if not panels:
        return

    pool = gevent.pool.Pool(size=10)

    def _poll_one(panel_id, url, token, name):
        try:
            client = FederationClient(url, token)
            data = client.snapshot()
            ts = data.get("timestamp")
            if ts and ts < 100_000_000_000:
                ts *= 1000
            ts = ts or int(time.time() * 1000)
            r = get_redis()
            prev = None
            if r:
                raw_prev = r.get(f"panel:{panel_id}:snapshot")
                if raw_prev:
                    try:
                        prev = json.loads(raw_prev)
                    except (TypeError, ValueError):
                        prev = None
            deltas = _diff_snapshots(prev, data)
            if r:
                r.setex(f"panel:{panel_id}:snapshot", 60, json.dumps(data))
                r.setex(f"panel:{panel_id}:status", 120, "online")
                r.setex(f"panel:{panel_id}:last_poll", 300, str(ts))
            return panel_id, "online", None, ts, deltas
        except Exception as exc:
            r = get_redis()
            if r:
                try:
                    r.setex(f"panel:{panel_id}:status", 120, "offline")
                except Exception:
                    pass
            return panel_id, "offline", str(exc)[:500], None, []

    jobs = []
    for p in panels:
        jobs.append(pool.spawn(_poll_one, p.id, p.url, p.federation_token, p.name))

    pool.join()

    dirty = False
    bucket = _ten_min_bucket(datetime.now())
    for job in jobs:
        panel_id, status, error, ts, deltas = job.value
        panel = db.session.get(LinkedPanel, panel_id)
        if panel is None:
            continue
        if status == "online" and deltas:
            for et, eid, itag, du, dd in deltas:
                _upsert_node_snapshot(panel_id, et, eid, itag, bucket, du, dd)
            dirty = True
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
