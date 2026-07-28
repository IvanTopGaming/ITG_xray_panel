import json
import logging
import time

import requests

from panel_core.extensions import db, get_shared_redis
from panel_core.models import LinkedPanel

logger = logging.getLogger(__name__)

_SNAPSHOT_TTL = 60
_STATUS_TTL = 120
_LAST_POLL_TTL = 300

REFRESH_CHANNEL = "panel:refresh"


class FederationClient:
    def __init__(self, url: str, federation_token: str) -> None:
        self.base_url = url.rstrip("/")
        self.token = federation_token
        self._session = requests.Session()
        self._session.headers["X-Federation-Token"] = self.token
        self._session.max_redirects = 0

    def _call(self, verb: str, path: str, **kwargs) -> dict:
        t0 = time.monotonic()
        try:
            resp = getattr(self._session, verb)(f"{self.base_url}{path}", **kwargs)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "federation %s %s failed in %.0f ms: %s",
                verb.upper(),
                path,
                (time.monotonic() - t0) * 1000,
                exc,
            )
            raise
        logger.debug(
            "federation %s %s -> HTTP %s in %.0f ms",
            verb.upper(),
            path,
            resp.status_code,
            (time.monotonic() - t0) * 1000,
        )
        return resp.json()

    def snapshot(self) -> dict:
        return self._call("get", "/api/federation/snapshot", timeout=(2, 5))

    def create_inbound(self, payload: dict) -> dict:
        return self._call("post", "/api/inbounds", json=payload, timeout=8)

    def update_inbound(self, tag: str, payload: dict) -> dict:
        return self._call("put", f"/api/inbounds/{tag}", json=payload, timeout=8)

    def delete_inbound(self, tag: str) -> dict:
        return self._call("delete", f"/api/inbounds/{tag}", timeout=8)

    def create_user(self, tag: str, user_data: dict) -> dict:
        return self._call("post", f"/api/inbounds/{tag}/users", json=user_data, timeout=8)

    def update_user(self, tag: str, user_data: dict) -> dict:
        return self._call("put", f"/api/inbounds/{tag}/users", json=user_data, timeout=8)

    def delete_user(self, tag: str, email: str) -> dict:
        return self._call("delete", f"/api/inbounds/{tag}/users", params={"email": email}, timeout=8)

    def bulk_delete_users(self, users: list) -> dict:
        return self._call("post", "/api/users/bulk-delete", json={"users": users}, timeout=30)

    def bulk_enable_users(self, users: list, enable: bool) -> dict:
        return self._call("post", "/api/users/bulk-enable", json={"users": users, "enable": enable}, timeout=30)

    def bulk_adjust_days(self, users: list, days: int, mode: str) -> dict:
        return self._call(
            "post", "/api/users/bulk-adjust-days", json={"users": users, "days": days, "mode": mode}, timeout=30
        )

    def bulk_adjust_traffic(self, users: list, gb: int, mode: str) -> dict:
        return self._call(
            "post", "/api/users/bulk-adjust-traffic", json={"users": users, "gb": gb, "mode": mode}, timeout=30
        )

    def reset_traffic(self, users: list) -> dict:
        return self._call("post", "/api/users/reset-traffic", json={"users": users}, timeout=30)

    def bulk_set_flow(self, users: list, flow: str) -> dict:
        return self._call("post", "/api/users/bulk-set-flow", json={"users": users, "flow": flow}, timeout=30)

    def provision(self, telegram_id: int, inbound_tag: str, params: dict) -> dict:
        return self._call(
            "post",
            "/api/federation/provision",
            json={"telegram_id": telegram_id, "inbound_tag": inbound_tag, **params},
            timeout=8,
        )


def _snapshot_key(panel_id: int) -> str:
    return f"panel:{panel_id}:snapshot"


def _status_key(panel_id: int) -> str:
    return f"panel:{panel_id}:status"


def _last_poll_key(panel_id: int) -> str:
    return f"panel:{panel_id}:last_poll"


def get_panel_snapshot(panel_id: int) -> dict | None:

    r = get_shared_redis()
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


def get_panel_liveness(panel_id: int) -> tuple[str | None, int | None]:

    r = get_shared_redis()
    if r is None:
        return None, None
    try:
        raw_status = r.get(_status_key(panel_id))
        status = None
        if raw_status is not None:
            status = raw_status.decode() if isinstance(raw_status, bytes) else str(raw_status)
        raw_poll = r.get(_last_poll_key(panel_id))
        last_poll = int(raw_poll) if raw_poll else None
        return status, last_poll
    except Exception as exc:
        logger.debug("panel_proxy: liveness read failed for panel %d: %s", panel_id, exc)
        return None, None


def store_panel_snapshot(panel_id: int, data: dict, last_poll_ms: int) -> None:

    r = get_shared_redis()
    if r is None:
        return
    try:
        r.setex(_snapshot_key(panel_id), _SNAPSHOT_TTL, json.dumps(data).encode())
        r.setex(_status_key(panel_id), _STATUS_TTL, "online")
        r.setex(_last_poll_key(panel_id), _LAST_POLL_TTL, str(last_poll_ms))
    except Exception as exc:
        logger.debug("panel_proxy: snapshot write failed for panel %d: %s", panel_id, exc)


def store_panel_offline(panel_id: int) -> None:

    r = get_shared_redis()
    if r is None:
        return
    try:
        r.setex(_status_key(panel_id), _STATUS_TTL, "offline")
    except Exception as exc:
        logger.debug("panel_proxy: offline marker write failed for panel %d: %s", panel_id, exc)


def forget_panel(panel_id: int) -> None:

    r = get_shared_redis()
    if r is None:
        return
    try:
        r.delete(_snapshot_key(panel_id), _status_key(panel_id), _last_poll_key(panel_id))
    except Exception as exc:
        logger.debug("panel_proxy: key removal failed for panel %d: %s", panel_id, exc)


def _nudge_panel_refresh(panel_id: int) -> None:

    r = get_shared_redis()
    if r is None:
        return
    try:
        r.publish(REFRESH_CHANNEL, str(panel_id))
    except Exception as exc:
        logger.debug("panel_proxy: refresh nudge failed for panel %d: %s", panel_id, exc)


def _get_panel_or_raise(panel_id: int) -> LinkedPanel:

    panel = db.session.get(LinkedPanel, panel_id)
    if panel is None:
        raise ValueError(f"Panel {panel_id} not found")
    if not panel.enable:
        raise ValueError(f"Panel '{panel.name}' is disabled")
    return panel


def proxy_create_user(panel_id: int, inbound_tag: str, user_data: dict) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.create_user(inbound_tag, user_data)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_update_user(panel_id: int, inbound_tag: str, user_data: dict) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.update_user(inbound_tag, user_data)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_delete_user(panel_id: int, inbound_tag: str, email: str) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.delete_user(inbound_tag, email)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_bulk_delete_users(panel_id: int, users: list) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.bulk_delete_users(users)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_bulk_enable_users(panel_id: int, users: list, enable: bool) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.bulk_enable_users(users, enable)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_bulk_adjust_days(panel_id: int, users: list, days: int, mode: str) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.bulk_adjust_days(users, days, mode)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_bulk_adjust_traffic(panel_id: int, users: list, gb: int, mode: str) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.bulk_adjust_traffic(users, gb, mode)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_bulk_reset_traffic(panel_id: int, users: list) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.reset_traffic(users)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_bulk_set_flow(panel_id: int, users: list, flow: str) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.bulk_set_flow(users, flow)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_create_inbound(panel_id: int, payload: dict) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.create_inbound(payload)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_update_inbound(panel_id: int, tag: str, payload: dict) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.update_inbound(tag, payload)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_delete_inbound(panel_id: int, tag: str) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.delete_inbound(tag)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_provision(
    panel_id: int,
    telegram_id: int,
    inbound_tag: str,
    params: dict,
) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.provision(telegram_id, inbound_tag, params)
    if not isinstance(result, dict) or result.get("expires_at_ms") is None:
        raise ValueError(
            f"Panel '{panel.name}' answered the provisioning request without an expiry. "
            f"It is running a federation contract this panel no longer speaks — update the node to the same "
            f"release as the master and retry."
        )
    _nudge_panel_refresh(panel.id)
    return result


def fetch_panel_snapshot_live(panel_id: int) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    return client.snapshot()
