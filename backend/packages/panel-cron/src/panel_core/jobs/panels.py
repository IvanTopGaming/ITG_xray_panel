import json
import logging
import time
from contextlib import nullcontext

import gevent
import gevent.pool
from flask import current_app

from panel_core.extensions import db, new_shared_redis_subscriber
from panel_core.models import LinkedPanel
from panel_core.services.panel_proxy import (
    REFRESH_CHANNEL,
    FederationClient,
    store_panel_offline,
    store_panel_snapshot,
)
from panel_core.services.state_mirror import read_current, write_cold, write_hot

logger = logging.getLogger(__name__)

_RECONNECT_DELAY = 5
_SHRINK_RATIO = 0.5
_ARCHIVE_KEEP_DAYS = 7
_TS_SECONDS_THRESHOLD = 100_000_000_000
_COLD_REFRESH_INTERVAL_MS = 15 * 60 * 1000
_TRANSFER_TOKEN_GRACE_MS = 24 * 3_600_000


def _normalize_ts(raw):
    ts = raw
    if ts and ts < _TS_SECONDS_THRESHOLD:
        ts *= 1000
    return ts or int(time.time() * 1000)


def _fetch_cold(url, token):
    return FederationClient(url, token).state()


def _client_count(inbounds):
    return sum(len(ib.get("clients") or []) for ib in inbounds)


def mirror_from_snapshot(panel_id, data):
    inbounds = data.get("inbounds")
    if not isinstance(inbounds, list):
        logger.warning("panel %s: snapshot has no usable inbounds list, mirror left untouched", panel_id)
        return

    panel = db.session.get(LinkedPanel, panel_id)
    if panel is None:
        return
    panel_url, panel_token = panel.url, panel.federation_token

    existing = read_current(panel_id)
    existing_fingerprint = existing.cold_fingerprint if existing is not None else None
    existing_has_cold = bool(existing.cold_state) if existing is not None else False
    existing_cold_updated_at = existing.cold_updated_at if existing is not None else None

    shrink = False
    if existing is not None and existing.hot_state:
        try:
            previous = json.loads(existing.hot_state).get("inbounds") or []
        except ValueError:
            previous = []
        was, now = _client_count(previous), _client_count(inbounds)
        if was and now < was * _SHRINK_RATIO:
            shrink = True
            logger.warning(
                "panel %s: client count fell from %s to %s in one poll; mirror written but flagged, "
                "the daily copies are the safety net",
                panel_id,
                was,
                now,
            )

    taken_at = _normalize_ts(data.get("timestamp"))
    write_hot(
        panel_id,
        {"inbounds": inbounds},
        taken_at=taken_at,
        instance_id=data.get("instance_id") or "",
        app_version=data.get("app_version") or "",
        shrink_flagged=shrink,
    )

    fingerprint = str(data.get("cold_fingerprint") or "")
    if not fingerprint:
        return

    cold_is_fresh = (
        existing_fingerprint == fingerprint
        and existing_has_cold
        and existing_cold_updated_at is not None
        and taken_at - existing_cold_updated_at < _COLD_REFRESH_INTERVAL_MS
    )
    if cold_is_fresh:
        return

    try:
        payload = _fetch_cold(panel_url, panel_token)
    except Exception as exc:
        logger.warning("panel %s: cold state fetch failed (%s); mirror keeps the previous copy", panel_id, exc)
        return

    write_cold(
        panel_id, payload.get("cold") or {}, fingerprint=payload.get("fingerprint") or fingerprint, taken_at=taken_at
    )


def _mirror_safely(panel_id, data, app):
    ctx = app.app_context() if app is not None else nullcontext()
    with ctx:
        try:
            mirror_from_snapshot(panel_id, data)
        except Exception:
            logger.warning("panel %s: state mirror write failed", panel_id, exc_info=True)
            try:
                db.session.rollback()
            except Exception:
                logger.warning("panel %s: state mirror rollback also failed", panel_id, exc_info=True)


def _poll_one(panel_id, url, token, app=None):
    try:
        client = FederationClient(url, token)
        data = client.snapshot()
        ts = _normalize_ts(data.get("timestamp"))
        store_panel_snapshot(panel_id, data, ts)
        _mirror_safely(panel_id, data, app)
        return panel_id, "online", None, ts
    except Exception as exc:
        store_panel_offline(panel_id)
        return panel_id, "offline", str(exc)[:500], None


def _record(results):
    dirty = False
    transfer_finished = []
    for result in results:
        if result is None:
            continue
        panel_id, status, error, ts = result
        panel = db.session.get(LinkedPanel, panel_id)
        if panel is None:
            continue

        if status == "offline" and (panel.transfer_state or "") == "awaiting_dns":
            status = "transferring"
        if status == "online" and (panel.transfer_state or ""):
            panel.transfer_state = ""
            dirty = True
            transfer_finished.append(panel.name)

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
        for name in transfer_finished:
            logger.info("panel %s: transfer finished, the A record has moved", name)


def poll_linked_panels():
    panels = LinkedPanel.query.filter_by(enable=True).all()
    if not panels:
        return

    app = current_app._get_current_object()
    pool = gevent.pool.Pool(size=10)
    jobs = [pool.spawn(_poll_one, p.id, p.url, p.federation_token, app) for p in panels]
    pool.join()

    _record([job.value for job in jobs])


def poll_panel_now(panel_id):
    panel = db.session.get(LinkedPanel, panel_id)
    if panel is None or not panel.enable:
        return
    _record([_poll_one(panel.id, panel.url, panel.federation_token)])


def archive_panel_state():
    from panel_core.services.state_mirror import archive_daily, prune_archive

    now = int(time.time() * 1000)
    for panel in LinkedPanel.query.all():
        try:
            archive_daily(panel.id, taken_at=now)
        except Exception:
            db.session.rollback()
            logger.warning("panel %s: daily state archive failed", panel.id, exc_info=True)

    try:
        cleared = LinkedPanel.query.filter(
            LinkedPanel.transfer_token.isnot(None),
            LinkedPanel.transfer_token_expires_at.isnot(None),
            LinkedPanel.transfer_token_expires_at < now - _TRANSFER_TOKEN_GRACE_MS,
        ).update({"transfer_token": None, "transfer_state": ""}, synchronize_session=False)
        db.session.commit()
        if cleared:
            logger.info("cleared %s expired transfer tokens", cleared)
    except Exception:
        db.session.rollback()
        logger.warning("clearing expired transfer tokens failed", exc_info=True)

    try:
        removed = prune_archive(older_than_ms=now - _ARCHIVE_KEEP_DAYS * 86_400_000)
    except Exception:
        db.session.rollback()
        logger.warning("state mirror archive prune failed", exc_info=True)
        return

    if removed:
        logger.info("pruned %s archived state mirrors older than %s days", removed, _ARCHIVE_KEEP_DAYS)


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
