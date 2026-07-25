import json
import logging
import time

import requests

from panel_core.extensions import db, get_redis
from panel_core.models import LinkedPanel

logger = logging.getLogger(__name__)

_SNAPSHOT_TTL = 60
_STATUS_TTL = 120


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


def get_panel_snapshot(panel_id: int) -> dict | None:

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

    client = FederationClient(panel.url, panel.federation_token)
    r = get_redis()
    try:
        data = client.snapshot()
    except Exception as exc:
        logger.info("panel_proxy: cache refresh failed for panel %d: %s", panel.id, exc)
        if r is not None:
            try:
                r.setex(_status_key(panel.id), _STATUS_TTL, "offline")
            except Exception:
                pass
        return

    if r is not None:
        try:
            encoded = json.dumps(data).encode()
            r.setex(_snapshot_key(panel.id), _SNAPSHOT_TTL, encoded)
            r.setex(_status_key(panel.id), _STATUS_TTL, "online")
        except Exception as exc:
            logger.debug("panel_proxy: Redis write failed for panel %d: %s", panel.id, exc)


def _get_panel_or_raise(panel_id: int) -> LinkedPanel:

    panel = db.session.get(LinkedPanel, panel_id)
    if panel is None:
        raise ValueError(f"Panel {panel_id} not found")
    if not panel.enable:
        raise ValueError(f"Panel '{panel.name}' is disabled")
    if panel.status == "offline":
        raise ValueError(f"Panel '{panel.name}' is offline")
    return panel


def proxy_create_user(panel_id: int, inbound_tag: str, user_data: dict) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.create_user(inbound_tag, user_data)
    _refresh_panel_cache(panel)
    return result


def proxy_update_user(panel_id: int, inbound_tag: str, user_data: dict) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.update_user(inbound_tag, user_data)
    _refresh_panel_cache(panel)
    return result


def proxy_delete_user(panel_id: int, inbound_tag: str, email: str) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.delete_user(inbound_tag, email)
    _refresh_panel_cache(panel)
    return result


def proxy_bulk_delete_users(panel_id: int, users: list) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.bulk_delete_users(users)
    _refresh_panel_cache(panel)
    return result


def proxy_bulk_enable_users(panel_id: int, users: list, enable: bool) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.bulk_enable_users(users, enable)
    _refresh_panel_cache(panel)
    return result


def proxy_bulk_adjust_days(panel_id: int, users: list, days: int, mode: str) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.bulk_adjust_days(users, days, mode)
    _refresh_panel_cache(panel)
    return result


def proxy_bulk_adjust_traffic(panel_id: int, users: list, gb: int, mode: str) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.bulk_adjust_traffic(users, gb, mode)
    _refresh_panel_cache(panel)
    return result


def proxy_bulk_reset_traffic(panel_id: int, users: list) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.reset_traffic(users)
    _refresh_panel_cache(panel)
    return result


def proxy_bulk_set_flow(panel_id: int, users: list, flow: str) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.bulk_set_flow(users, flow)
    _refresh_panel_cache(panel)
    return result


def proxy_create_inbound(panel_id: int, payload: dict) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.create_inbound(payload)
    _refresh_panel_cache(panel)
    return result


def proxy_update_inbound(panel_id: int, tag: str, payload: dict) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.update_inbound(tag, payload)
    _refresh_panel_cache(panel)
    return result


def proxy_delete_inbound(panel_id: int, tag: str) -> dict:

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

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.provision(telegram_id, inbound_tag, params)
    _refresh_panel_cache(panel)
    return result


def fetch_panel_snapshot_live(panel_id: int) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    return client.snapshot()
