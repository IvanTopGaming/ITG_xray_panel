"""Federation HTTP client and proxy layer for master→child panel communication.

Each proxy_* function is the single call-site for an operation that must be
forwarded to a child (LinkedPanel).  It validates panel reachability, delegates
to FederationClient, then refreshes the panel's Redis snapshot so callers
always have a reasonably fresh picture of the child's state.
"""

import json
import logging
import time

import requests

from app.extensions import db, get_redis
from app.models import LinkedPanel

logger = logging.getLogger(__name__)

_SNAPSHOT_TTL = 60  # seconds — Redis TTL for panel:{id}:snapshot
_STATUS_TTL = 120  # seconds — Redis TTL for panel:{id}:status


# ─── HTTP client ──────────────────────────────────────────────────────────────


class FederationClient:
    """Synchronous HTTP client for a single child panel.

    All requests carry the shared federation token in the
    ``X-Federation-Token`` header so the child can authenticate the master
    without a per-session login round-trip.
    """

    def __init__(self, url: str, federation_token: str) -> None:
        self.base_url = url.rstrip("/")
        self.token = federation_token
        self._session = requests.Session()
        self._session.headers["X-Federation-Token"] = self.token

    # ── snapshot ──────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """GET /api/federation/snapshot — returns the child's current state.

        Called every 10s by `poll_linked_panels`. Uses a split `(connect, read)`
        timeout so an offline panel fails fast on connect (2s) instead of
        blocking the polling cycle for the full read budget.
        """
        resp = self._session.get(f"{self.base_url}/api/federation/snapshot", timeout=(2, 5))
        resp.raise_for_status()
        return resp.json()

    # ── inbound CRUD ──────────────────────────────────────────────────────

    def create_inbound(self, payload: dict) -> dict:
        """POST /api/inbounds — create an inbound on the child panel."""
        resp = self._session.post(
            f"{self.base_url}/api/inbounds",
            json=payload,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def update_inbound(self, tag: str, payload: dict) -> dict:
        """PUT /api/inbounds/{tag} — update an inbound on the child panel."""
        resp = self._session.put(
            f"{self.base_url}/api/inbounds/{tag}",
            json=payload,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_inbound(self, tag: str) -> dict:
        """DELETE /api/inbounds/{tag} — remove an inbound from the child panel."""
        resp = self._session.delete(
            f"{self.base_url}/api/inbounds/{tag}",
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    # ── user CRUD ─────────────────────────────────────────────────────────

    def create_user(self, tag: str, user_data: dict) -> dict:
        """POST /api/inbounds/{tag}/users — add a user to an inbound."""
        resp = self._session.post(
            f"{self.base_url}/api/inbounds/{tag}/users",
            json=user_data,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def update_user(self, tag: str, user_data: dict) -> dict:
        """PUT /api/inbounds/{tag}/users — update a user in an inbound."""
        resp = self._session.put(
            f"{self.base_url}/api/inbounds/{tag}/users",
            json=user_data,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_user(self, tag: str, email: str) -> dict:
        """DELETE /api/inbounds/{tag}/users?email=... — remove a user."""
        resp = self._session.delete(
            f"{self.base_url}/api/inbounds/{tag}/users",
            params={"email": email},
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    # ── provisioning ──────────────────────────────────────────────────────

    def provision(self, telegram_id: int, inbound_tag: str, params: dict) -> dict:
        """POST /api/federation/provision — apply a tariff grant on the child."""
        resp = self._session.post(
            f"{self.base_url}/api/federation/provision",
            json={"telegram_id": telegram_id, "inbound_tag": inbound_tag, **params},
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()


# ─── Redis helpers ────────────────────────────────────────────────────────────


def _snapshot_key(panel_id: int) -> str:
    return f"panel:{panel_id}:snapshot"


def _status_key(panel_id: int) -> str:
    return f"panel:{panel_id}:status"


def get_panel_snapshot(panel_id: int) -> dict | None:
    """Return the cached snapshot dict from Redis, or None on miss / no Redis."""
    r = get_redis()
    if r is None:
        return None
    try:
        raw = r.get(_snapshot_key(panel_id))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.debug("panel_proxy: snapshot cache read failed for panel %d: %s", panel_id, exc)
        return None


def _refresh_panel_cache(panel: LinkedPanel) -> None:
    """Fetch a fresh snapshot from *panel* and write it to Redis + DB.

    On success:  stores snapshot in Redis (TTL=60s), status "online" (TTL=120s),
                 sets panel.status="online" and panel.last_poll.
    On failure:  sets panel.status="offline" and panel.last_error, commits,
                 raises the original exception so callers know the panel is down.
    """
    client = FederationClient(panel.url, panel.federation_token)
    now = int(time.time())
    try:
        data = client.snapshot()
    except Exception as exc:
        panel.status = "offline"
        panel.last_poll = now
        panel.last_error = str(exc)
        db.session.commit()

        r = get_redis()
        if r is not None:
            try:
                r.setex(_status_key(panel.id), _STATUS_TTL, "offline")
            except Exception:
                pass
        raise

    panel.status = "online"
    panel.last_poll = now
    panel.last_error = None
    db.session.commit()

    r = get_redis()
    if r is not None:
        try:
            encoded = json.dumps(data).encode()
            r.setex(_snapshot_key(panel.id), _SNAPSHOT_TTL, encoded)
            r.setex(_status_key(panel.id), _STATUS_TTL, "online")
        except Exception as exc:
            logger.debug("panel_proxy: Redis write failed for panel %d: %s", panel.id, exc)


# ─── Internal helpers ─────────────────────────────────────────────────────────


def _get_panel_or_raise(panel_id: int) -> LinkedPanel:
    """Return the LinkedPanel row or raise ValueError.

    Raises ValueError when:
    - the row does not exist
    - panel.enable is False
    - panel.status is "offline"
    """
    panel = db.session.get(LinkedPanel, panel_id)
    if panel is None:
        raise ValueError(f"Panel {panel_id} not found")
    if not panel.enable:
        raise ValueError(f"Panel '{panel.name}' is disabled")
    if panel.status == "offline":
        raise ValueError(f"Panel '{panel.name}' is offline")
    return panel


# ─── Proxy operations ─────────────────────────────────────────────────────────


def proxy_create_user(panel_id: int, inbound_tag: str, user_data: dict) -> dict:
    """Create a user on the given child panel and refresh the snapshot cache."""
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.create_user(inbound_tag, user_data)
    _refresh_panel_cache(panel)
    return result


def proxy_update_user(panel_id: int, inbound_tag: str, user_data: dict) -> dict:
    """Update a user on the given child panel and refresh the snapshot cache."""
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.update_user(inbound_tag, user_data)
    _refresh_panel_cache(panel)
    return result


def proxy_delete_user(panel_id: int, inbound_tag: str, email: str) -> dict:
    """Delete a user from the given child panel and refresh the snapshot cache."""
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.delete_user(inbound_tag, email)
    _refresh_panel_cache(panel)
    return result


def proxy_create_inbound(panel_id: int, payload: dict) -> dict:
    """Create an inbound on the given child panel and refresh the snapshot cache."""
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.create_inbound(payload)
    _refresh_panel_cache(panel)
    return result


def proxy_update_inbound(panel_id: int, tag: str, payload: dict) -> dict:
    """Update an inbound on the given child panel and refresh the snapshot cache."""
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.update_inbound(tag, payload)
    _refresh_panel_cache(panel)
    return result


def proxy_delete_inbound(panel_id: int, tag: str) -> dict:
    """Delete an inbound from the given child panel and refresh the snapshot cache."""
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.delete_inbound(tag)
    _refresh_panel_cache(panel)
    return result


def proxy_provision(
    panel_id: int,
    telegram_id: int,
    inbound_tag: str,
    params: dict,
) -> dict:
    """Apply a tariff grant on the given child panel and refresh the snapshot cache."""
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.provision(telegram_id, inbound_tag, params)
    _refresh_panel_cache(panel)
    return result


def fetch_panel_snapshot_live(panel_id: int) -> dict:
    """Fetch a child panel's snapshot directly (no Redis cache).

    Raises ValueError if the panel is missing/disabled/offline and propagates
    any HTTP error from FederationClient — callers treat a raise as
    "panel unreachable".
    """
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    return client.snapshot()
