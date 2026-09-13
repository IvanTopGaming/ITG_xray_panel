import json
import logging
import time
from urllib.parse import urlparse

from flask import g, has_request_context

from panel_core.extensions import db
from panel_core.models import LinkedPanel
from panel_core.services import panel_proxy


logger = logging.getLogger(__name__)


class SubscriptionUnavailable(RuntimeError):
    pass


class UnsupportedSubscriptionFormat(SubscriptionUnavailable):
    pass


def access_reason(client, *, blocked=False):
    if blocked:
        return "blocked"
    field = client.get if isinstance(client, dict) else lambda key, default=None: getattr(client, key, default)
    expiry = field("expiry_time")
    if expiry is not None and int(expiry) != 0 and int(expiry) <= int(time.time() * 1000):
        return "expired"
    limit = int(field("limit_bytes", 0) or 0)
    if limit > 0 and int(field("up", 0) or 0) + int(field("down", 0) or 0) >= limit:
        return "traffic_exhausted"
    if not field("enable", True):
        return "disabled"
    return "active"


def aggregate_access_reason(clients, *, blocked=False):
    if blocked:
        return "blocked"
    reasons = {access_reason(client) for client in clients}
    for reason in ("active", "disabled", "traffic_exhausted", "expired"):
        if reason in reasons:
            return reason
    return "not_configured"


def _sources():
    if has_request_context() and hasattr(g, "subscription_sources"):
        return g.subscription_sources
    sources = []
    panels = LinkedPanel.query.filter_by(enable=True).options(db.defer(LinkedPanel.federation_token)).all()
    for panel in panels:
        snapshot = panel_proxy.get_panel_snapshot(panel.id)
        try:
            host = urlparse(panel.url).hostname
        except ValueError:
            host = None
        sources.append((panel.id, host, snapshot))
    if has_request_context():
        g.subscription_sources = sources
    return sources


def remote_subscription_clients(*, telegram_id=None, client_uuid=None, only_enabled=True):
    failures = set()
    for panel_id, host, snapshot in _sources():
        if not host or not isinstance(snapshot, dict) or not isinstance(snapshot.get("inbounds"), list):
            failures.add(panel_id)
            continue
        for inbound in snapshot["inbounds"]:
            if not isinstance(inbound, dict) or not isinstance(inbound.get("clients"), list):
                failures.add(panel_id)
                continue
            for client in inbound["clients"]:
                if not isinstance(client, dict):
                    failures.add(panel_id)
                    continue
                if telegram_id is not None and client.get("telegram_id") != telegram_id:
                    continue
                if client_uuid is not None and client.get("id") != client_uuid:
                    continue
                if only_enabled and access_reason(client) != "active":
                    continue
                stream = inbound.get("stream_settings", {})
                try:
                    if isinstance(stream, str):
                        stream = json.loads(stream)
                    if not isinstance(stream, dict):
                        raise ValueError("stream settings must be an object")
                except (TypeError, ValueError):
                    failures.add(panel_id)
                    continue
                yield host, {**inbound, "panel_id": panel_id}, client, stream
    if failures:
        panels = ",".join(str(panel_id) for panel_id in sorted(failures))
        logger.error("subscription source unavailable or invalid: panel_ids=%s phase=snapshot", panels)
        raise SubscriptionUnavailable(f"Subscription data is temporarily unavailable (panels: {panels})")
