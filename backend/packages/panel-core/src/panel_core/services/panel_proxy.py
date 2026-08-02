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
_STATS_TIMEOUT = 15
_XRAY_RESTART_TIMEOUT = 30
_GEO_TIMEOUT = 110

REFRESH_CHANNEL = "panel:refresh"

STALE_STATUS = "stale"

_stale_warned: set[int] = set()


class RemotePanelError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class FederationClient:
    def __init__(self, url: str, federation_token: str) -> None:
        self.base_url = url.rstrip("/")
        self.token = federation_token
        self._session = requests.Session()
        self._session.headers["X-Federation-Token"] = self.token
        self._session.max_redirects = 0

    def _call_reporting(self, verb: str, path: str, **kwargs):

        t0 = time.monotonic()
        try:
            resp = getattr(self._session, verb)(f"{self.base_url}{path}", **kwargs)
        except Exception as exc:
            logger.warning(
                "federation %s %s unreachable in %.0f ms: %s",
                verb.upper(),
                path,
                (time.monotonic() - t0) * 1000,
                exc,
            )
            raise RemotePanelError(502, f"Panel is unreachable: {exc}") from exc

        if resp.status_code >= 400:
            body = {}
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    body = parsed
            except Exception:
                body = {}
            message = str(body.get("error") or body.get("message") or "").strip()
            logger.warning(
                "federation %s %s -> HTTP %s: %s",
                verb.upper(),
                path,
                resp.status_code,
                message or "(no message)",
            )
            raise RemotePanelError(resp.status_code, message or f"Panel answered HTTP {resp.status_code}")

        logger.debug(
            "federation %s %s -> HTTP %s in %.0f ms",
            verb.upper(),
            path,
            resp.status_code,
            (time.monotonic() - t0) * 1000,
        )
        try:
            return resp.json()
        except Exception as exc:
            raise RemotePanelError(502, "Panel answered with something that is not JSON") from exc

    def snapshot(self) -> dict:
        return self._call_reporting("get", "/api/federation/snapshot", timeout=(2, 5))

    def list_outbounds(self) -> list:
        return self._call_reporting("get", "/api/outbounds", timeout=8)

    def create_outbound(self, payload: dict) -> dict:
        return self._call_reporting("post", "/api/outbounds", json=payload, timeout=8)

    def update_outbound(self, tag: str, payload: dict) -> dict:
        return self._call_reporting("put", f"/api/outbounds/{tag}", json=payload, timeout=8)

    def delete_outbound(self, tag: str) -> dict:
        return self._call_reporting("delete", f"/api/outbounds/{tag}", timeout=8)

    def list_balancers(self) -> list:
        return self._call_reporting("get", "/api/balancers", timeout=8)

    def create_balancer(self, payload: dict) -> dict:
        return self._call_reporting("post", "/api/balancers", json=payload, timeout=8)

    def update_balancer(self, tag: str, payload: dict) -> dict:
        return self._call_reporting("put", f"/api/balancers/{tag}", json=payload, timeout=8)

    def delete_balancer(self, tag: str) -> dict:
        return self._call_reporting("delete", f"/api/balancers/{tag}", timeout=8)

    def list_routing_profiles(self) -> list:
        return self._call_reporting("get", "/api/routing-profiles", timeout=8)

    def create_routing_profile(self, payload: dict) -> dict:
        return self._call_reporting("post", "/api/routing-profiles", json=payload, timeout=8)

    def update_routing_profile(self, profile_id: int, payload: dict) -> dict:
        return self._call_reporting("put", f"/api/routing-profiles/{profile_id}", json=payload, timeout=8)

    def delete_routing_profile(self, profile_id: int) -> dict:
        return self._call_reporting("delete", f"/api/routing-profiles/{profile_id}", timeout=8)

    def reset_inbound_traffic(self, tag: str) -> dict:
        return self._call_reporting("post", f"/api/inbounds/{tag}/reset-traffic", timeout=30)

    def stats_overview(self, params: dict) -> dict:
        return self._call_reporting("get", "/api/stats/overview", params=params, timeout=_STATS_TIMEOUT)

    def stats_traffic(self, params: dict) -> dict:
        return self._call_reporting("get", "/api/stats/traffic", params=params, timeout=_STATS_TIMEOUT)

    def stats_domains(self, params: dict) -> dict:
        return self._call_reporting("get", "/api/stats/domains", params=params, timeout=_STATS_TIMEOUT)

    def stats_domain_users(self, params: dict) -> dict:
        return self._call_reporting("get", "/api/stats/domain-users", params=params, timeout=_STATS_TIMEOUT)

    def stats_users_ranking(self, params: dict) -> dict:
        return self._call_reporting("get", "/api/stats/users-ranking", params=params, timeout=_STATS_TIMEOUT)

    def get_system_settings(self) -> dict:
        return self._call_reporting("get", "/api/system/settings", timeout=8)

    def update_system_settings(self, payload: dict) -> dict:
        return self._call_reporting("put", "/api/system/settings", json=payload, timeout=_XRAY_RESTART_TIMEOUT)

    def get_xray_config(self) -> dict:
        return self._call_reporting("get", "/api/config", timeout=15)

    def update_geo(self) -> dict:
        return self._call_reporting("post", "/api/system/update-geo", timeout=_GEO_TIMEOUT)

    def restart_xray(self) -> dict:
        return self._call_reporting("post", "/api/restart", timeout=_XRAY_RESTART_TIMEOUT)

    def set_user_routing(self, payload: dict) -> dict:
        return self._call_reporting("post", "/api/user/routing", json=payload, timeout=_XRAY_RESTART_TIMEOUT)

    def create_inbound(self, payload: dict) -> dict:
        return self._call_reporting("post", "/api/inbounds", json=payload, timeout=8)

    def update_inbound(self, tag: str, payload: dict) -> dict:
        return self._call_reporting("put", f"/api/inbounds/{tag}", json=payload, timeout=8)

    def delete_inbound(self, tag: str) -> dict:
        return self._call_reporting("delete", f"/api/inbounds/{tag}", timeout=8)

    def create_user(self, tag: str, user_data: dict) -> dict:
        return self._call_reporting("post", f"/api/inbounds/{tag}/users", json=user_data, timeout=8)

    def update_user(self, tag: str, user_data: dict) -> dict:
        return self._call_reporting("put", f"/api/inbounds/{tag}/users", json=user_data, timeout=8)

    def delete_user(self, tag: str, email: str) -> dict:
        return self._call_reporting("delete", f"/api/inbounds/{tag}/users", params={"email": email}, timeout=8)

    def bulk_delete_users(self, users: list) -> dict:
        return self._call_reporting("post", "/api/users/bulk-delete", json={"users": users}, timeout=30)

    def bulk_enable_users(self, users: list, enable: bool) -> dict:
        return self._call_reporting(
            "post", "/api/users/bulk-enable", json={"users": users, "enable": enable}, timeout=30
        )

    def bulk_adjust_days(self, users: list, days: int, mode: str) -> dict:
        return self._call_reporting(
            "post", "/api/users/bulk-adjust-days", json={"users": users, "days": days, "mode": mode}, timeout=30
        )

    def bulk_adjust_traffic(self, users: list, gb: int, mode: str) -> dict:
        return self._call_reporting(
            "post", "/api/users/bulk-adjust-traffic", json={"users": users, "gb": gb, "mode": mode}, timeout=30
        )

    def reset_traffic(self, users: list) -> dict:
        return self._call_reporting("post", "/api/users/reset-traffic", json={"users": users}, timeout=30)

    def bulk_set_flow(self, users: list, flow: str) -> dict:
        return self._call_reporting("post", "/api/users/bulk-set-flow", json={"users": users, "flow": flow}, timeout=30)

    def provision(self, telegram_id: int, inbound_tag: str, params: dict) -> dict:
        return self._call_reporting(
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


def _last_snapshot_key(panel_id: int) -> str:
    return f"panel:{panel_id}:snapshot:last"


def _last_seen_key(panel_id: int) -> str:
    return f"panel:{panel_id}:last_poll:last"


def _decode_int(raw) -> int | None:
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _warn_stale(panel_id: int, last_seen_ms: int | None) -> None:
    if panel_id in _stale_warned:
        return
    _stale_warned.add(panel_id)
    logger.warning(
        "panel_proxy: panel %d has no fresh snapshot — nothing has refreshed it within %d s, so the cron "
        "poller is not reaching it. Serving the last known copy (taken %s). Users are being handed node "
        "entries that may be out of date, and a client disabled since then is still being served.",
        panel_id,
        _SNAPSHOT_TTL,
        f"at {last_seen_ms} ms" if last_seen_ms else "at an unrecorded time",
    )


def get_panel_snapshot(panel_id: int) -> dict | None:

    r = get_shared_redis()
    if r is None:
        return None
    try:
        raw = r.get(_snapshot_key(panel_id))
        if raw is not None:
            _stale_warned.discard(panel_id)
            return json.loads(raw)
        raw = r.get(_last_snapshot_key(panel_id))
        if raw is None:
            return None
        data = json.loads(raw)
        _warn_stale(panel_id, _decode_int(r.get(_last_seen_key(panel_id))))
        return data
    except Exception as exc:
        logger.debug("panel_proxy: snapshot cache read failed for panel %d: %s", panel_id, exc)
        return None


def get_panel_liveness(panel_id: int) -> tuple[str | None, int | None]:

    r = get_shared_redis()
    if r is None:
        return None, None
    try:
        raw_status = r.get(_status_key(panel_id))
        if raw_status is not None:
            status = raw_status.decode() if isinstance(raw_status, bytes) else str(raw_status)
            return status, _decode_int(r.get(_last_poll_key(panel_id)))
        if r.get(_last_snapshot_key(panel_id)) is None:
            return None, None
        return STALE_STATUS, _decode_int(r.get(_last_seen_key(panel_id)))
    except Exception as exc:
        logger.debug("panel_proxy: liveness read failed for panel %d: %s", panel_id, exc)
        return None, None


def store_panel_snapshot(panel_id: int, data: dict, last_poll_ms: int) -> None:

    r = get_shared_redis()
    if r is None:
        return
    try:
        payload = json.dumps(data).encode()
        r.setex(_snapshot_key(panel_id), _SNAPSHOT_TTL, payload)
        r.setex(_status_key(panel_id), _STATUS_TTL, "online")
        r.setex(_last_poll_key(panel_id), _LAST_POLL_TTL, str(last_poll_ms))
        r.set(_last_snapshot_key(panel_id), payload)
        r.set(_last_seen_key(panel_id), str(last_poll_ms))
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
    _stale_warned.discard(panel_id)
    try:
        r.delete(
            _snapshot_key(panel_id),
            _status_key(panel_id),
            _last_poll_key(panel_id),
            _last_snapshot_key(panel_id),
            _last_seen_key(panel_id),
        )
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
    try:
        result = client.provision(telegram_id, inbound_tag, params)
    except RemotePanelError as exc:
        raise RemotePanelError(exc.status_code, f"Panel '{panel.name}': {exc.message}") from exc
    if not isinstance(result, dict) or result.get("expires_at_ms") is None:
        raise ValueError(
            f"Panel '{panel.name}' answered the provisioning request without an expiry. "
            f"It is running a federation contract this panel no longer speaks — update the node to the same "
            f"release as the master and retry."
        )
    _nudge_panel_refresh(panel.id)
    return result


def _client_for(panel_id: int) -> tuple[LinkedPanel, FederationClient]:

    panel = _get_panel_or_raise(panel_id)
    return panel, FederationClient(panel.url, panel.federation_token)


def proxy_list_outbounds(panel_id: int) -> list:

    _, client = _client_for(panel_id)
    return client.list_outbounds()


def proxy_create_outbound(panel_id: int, payload: dict) -> dict:

    panel, client = _client_for(panel_id)
    result = client.create_outbound(payload)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_update_outbound(panel_id: int, tag: str, payload: dict) -> dict:

    panel, client = _client_for(panel_id)
    result = client.update_outbound(tag, payload)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_delete_outbound(panel_id: int, tag: str) -> dict:

    panel, client = _client_for(panel_id)
    result = client.delete_outbound(tag)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_list_balancers(panel_id: int) -> list:

    _, client = _client_for(panel_id)
    return client.list_balancers()


def proxy_create_balancer(panel_id: int, payload: dict) -> dict:

    panel, client = _client_for(panel_id)
    result = client.create_balancer(payload)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_update_balancer(panel_id: int, tag: str, payload: dict) -> dict:

    panel, client = _client_for(panel_id)
    result = client.update_balancer(tag, payload)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_delete_balancer(panel_id: int, tag: str) -> dict:

    panel, client = _client_for(panel_id)
    result = client.delete_balancer(tag)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_list_routing_profiles(panel_id: int) -> list:

    _, client = _client_for(panel_id)
    return client.list_routing_profiles()


def proxy_create_routing_profile(panel_id: int, payload: dict) -> dict:

    panel, client = _client_for(panel_id)
    result = client.create_routing_profile(payload)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_update_routing_profile(panel_id: int, profile_id: int, payload: dict) -> dict:

    panel, client = _client_for(panel_id)
    result = client.update_routing_profile(profile_id, payload)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_delete_routing_profile(panel_id: int, profile_id: int) -> dict:

    panel, client = _client_for(panel_id)
    result = client.delete_routing_profile(profile_id)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_reset_inbound_traffic(panel_id: int, tag: str) -> dict:

    panel, client = _client_for(panel_id)
    result = client.reset_inbound_traffic(tag)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_stats_overview(panel_id: int, params: dict) -> dict:

    _, client = _client_for(panel_id)
    return client.stats_overview(params)


def proxy_stats_traffic(panel_id: int, params: dict) -> dict:

    _, client = _client_for(panel_id)
    return client.stats_traffic(params)


def proxy_stats_domains(panel_id: int, params: dict) -> dict:

    _, client = _client_for(panel_id)
    return client.stats_domains(params)


def proxy_stats_domain_users(panel_id: int, params: dict) -> dict:

    _, client = _client_for(panel_id)
    return client.stats_domain_users(params)


def proxy_stats_users_ranking(panel_id: int, params: dict) -> dict:

    _, client = _client_for(panel_id)
    return client.stats_users_ranking(params)


def proxy_get_system_settings(panel_id: int) -> dict:

    _, client = _client_for(panel_id)
    return client.get_system_settings()


def proxy_update_system_settings(panel_id: int, payload: dict) -> dict:

    panel, client = _client_for(panel_id)
    result = client.update_system_settings(payload)
    _nudge_panel_refresh(panel.id)
    return result


def proxy_get_xray_config(panel_id: int) -> dict:

    _, client = _client_for(panel_id)
    return client.get_xray_config()


def proxy_update_geo(panel_id: int) -> dict:

    panel, client = _client_for(panel_id)
    result = client.update_geo()
    _nudge_panel_refresh(panel.id)
    return result


def proxy_restart_xray(panel_id: int) -> dict:

    panel, client = _client_for(panel_id)
    result = client.restart_xray()
    _nudge_panel_refresh(panel.id)
    return result


def proxy_set_user_routing(panel_id: int, payload: dict) -> dict:

    panel, client = _client_for(panel_id)
    result = client.set_user_routing(payload)
    _nudge_panel_refresh(panel.id)
    return result


def fetch_panel_snapshot_live(panel_id: int) -> dict:

    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    return client.snapshot()
