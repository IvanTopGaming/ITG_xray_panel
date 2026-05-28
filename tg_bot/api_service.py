import aiohttp
import logging
import asyncio
import base64
import binascii
import json
import os
import re
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)


def _is_error_response(payload):
    return isinstance(payload, dict) and "error" in payload


def _extract_inbounds(payload):
    if isinstance(payload, list):
        return payload
    return None


def _operation_failed(result):
    return result is None or isinstance(result, Exception) or _is_error_response(result)


_ENDPOINT_RE = re.compile(r"^(?P<scheme>vless|trojan|ss)://[^@\s]+@(?P<host>[^:/?#\s]+):(?P<port>\d+)", re.IGNORECASE)


def _parse_link_endpoint(link):
    """Extract (scheme, host, port) from a subscription URI.

    Returns a lowercased 3-tuple or None if the link is not parseable.
    VMess is handled separately because its endpoint is inside base64(JSON).
    """
    if not isinstance(link, str) or not link:
        return None
    stripped = link.strip()
    if stripped.lower().startswith("vmess://"):
        return _parse_vmess_endpoint(stripped)
    m = _ENDPOINT_RE.match(stripped)
    if not m:
        return None
    try:
        port = int(m.group("port"))
    except ValueError:
        return None
    return (m.group("scheme").lower(), m.group("host").lower(), port)


def _parse_vmess_endpoint(link):
    payload_b64 = link[len("vmess://") :].strip()
    if not payload_b64:
        return None
    try:
        raw = base64.b64decode(payload_b64, validate=False).decode("utf-8", errors="strict")
        data = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    host = data.get("add")
    port_raw = data.get("port")
    if not isinstance(host, str) or not host:
        return None
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        return None
    return ("vmess", host.lower(), port)


def _panel_hostname(panel):
    """Return the lowercased hostname of a SinglePanel's base_url, or None."""
    base = getattr(panel, "base_url", "") or ""
    if not base:
        return None
    try:
        parsed = urlparse(base)
    except ValueError:
        return None
    host = (parsed.hostname or "").strip().lower()
    return host or None


def _dedup_pairs(pairs):
    """Collapse (panel, link) pairs that share the same (scheme, host, port).

    When multiple pairs share an endpoint, prefer the one whose panel's own
    hostname equals the link's host ("direct"). Unparseable links are kept
    as-is since we can't compare them.
    """
    kept_order = []
    kept_by_key = {}
    unparseable = []

    for panel, link in pairs:
        endpoint = _parse_link_endpoint(link)
        if endpoint is None:
            unparseable.append((panel, link))
            continue

        if endpoint not in kept_by_key:
            kept_order.append(endpoint)
            kept_by_key[endpoint] = (panel, link)
            continue

        existing_panel, _existing_link = kept_by_key[endpoint]
        existing_is_direct = _panel_hostname(existing_panel) == endpoint[1]
        current_is_direct = _panel_hostname(panel) == endpoint[1]
        if current_is_direct and not existing_is_direct:
            kept_by_key[endpoint] = (panel, link)

    result = [kept_by_key[k] for k in kept_order]
    result.extend(unparseable)
    return result


class SinglePanel:
    def __init__(self, conf):
        self.name = conf["name"]
        self.base_url = conf["url"].rstrip("/")
        self.token = conf["token"]
        self.target_inbound = conf["inbound_tag"]
        self.role = conf.get("role", "standalone")
        self.session = None

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None

    async def health_check(self):
        """Quick auth-checked ping; returns True if panel accepts our token."""
        await self.ensure_session()
        if not self.token:
            return False
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with self.session.get(
                f"{self.base_url}/api/stats/system",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def request(self, method, endpoint, json_data=None, params=None, data=None, timeout=None):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return await self._execute_request(method, endpoint, json_data, params, data, timeout)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                err = str(e) or type(e).__name__
                logger.info(
                    "[%s] Request %s failed (attempt %d/%d): %s", self.name, endpoint, attempt + 1, max_retries, err
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                else:
                    return None
            except Exception:
                logger.exception("[%s] Unexpected error %s", self.name, endpoint)
                return None

    async def _execute_request(self, method, endpoint, json_data, params, data, timeout=None):
        await self.ensure_session()
        if not self.token:
            return {"error": "Auth Failed"}

        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.base_url}/api/{endpoint}"
        req_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else None

        async with self.session.request(
            method, url, json=json_data, params=params, data=data, headers=headers, timeout=req_timeout
        ) as resp:
            if resp.status == 401:
                logger.warning("[%s] bot_service_token rejected by panel", self.name)
                return {"error": "Auth Failed"}
            return await self._process_response(resp)

    async def _process_response(self, resp):
        if resp.status in [200, 201]:
            if resp.content_type == "application/json":
                return await resp.json()
            elif "text" in resp.content_type:
                return await resp.text()
            return await resp.read()

        try:
            error_text = await resp.json()
            error_msg = error_text.get("error", resp.reason)
        except (aiohttp.ContentTypeError, ValueError):
            error_msg = f"HTTP {resp.status}"

        return {"error": error_msg, "status": resp.status}


class MultiPanelManager:
    def __init__(self):
        # Single-master topology: panels[] always has zero or one entry,
        # rebuilt from runtime_config (panel admin user/password + the
        # backend URL the bot is configured to talk to). Filled in on
        # first reload_from_runtime() call.
        self.panels: list[SinglePanel] = []

    @staticmethod
    def _normalize_inbound_tag(value):
        tag = str(value or "").strip()
        if not tag or tag.lower() == "multi":
            return ""
        return tag

    def _resolve_inbound_tag(self, panel, inbound_tag=None):
        normalized = self._normalize_inbound_tag(inbound_tag)
        return normalized or panel.target_inbound

    async def close(self):
        tasks = [panel.close() for panel in self.panels]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reload_from_runtime(self):
        """Rebuild the single master panel from env (BACKEND_API_URL + BOT_SERVICE_TOKEN)."""
        new_panels: list[SinglePanel] = []
        backend_url = os.environ.get("BACKEND_API_URL", "").rstrip("/")
        token = os.environ.get("BOT_SERVICE_TOKEN", "")
        if backend_url and token:
            # strip trailing /api so SinglePanel can re-add it
            base = backend_url[:-4] if backend_url.endswith("/api") else backend_url
            new_panels.append(
                SinglePanel(
                    {
                        "name": "master",
                        "url": base,
                        "token": token,
                        "inbound_tag": "",
                        "role": "master",
                    }
                )
            )
        old = self.panels
        self.panels = new_panels
        if old:
            await asyncio.gather(*[p.close() for p in old], return_exceptions=True)

    async def get_linked_panels(self):
        if not self.panels:
            return []
        try:
            res = await self.panels[0].request("GET", "panels")
            return res if isinstance(res, list) else []
        except Exception:
            return []

    async def get_system_stats_all(self):
        stats_list = []
        for p in self.panels:
            res = await p.request("GET", "stats/system")
            if isinstance(res, dict) and "error" not in res:
                res["server_name"] = p.name
                stats_list.append(res)
            else:
                stats_list.append({"server_name": p.name, "error": True})

        linked = await self.get_linked_panels()
        for lp in linked:
            if not lp.get("enable", True):
                continue
            panel_id = lp.get("id")
            name = lp.get("name", f"Panel {panel_id}")
            try:
                res = await self.panels[0].request("GET", f"panels/{panel_id}/system-stats")
                if isinstance(res, dict) and "error" not in res:
                    res["server_name"] = name
                    res["panel_id"] = panel_id
                    stats_list.append(res)
                else:
                    stats_list.append({"server_name": name, "panel_id": panel_id, "error": True})
            except Exception:
                stats_list.append({"server_name": name, "panel_id": panel_id, "error": True})

        return stats_list

    async def restart_all(self):
        tasks = [p.request("POST", "restart") for p in self.panels]

        linked = await self.get_linked_panels()
        for lp in linked:
            if lp.get("enable", True):
                tasks.append(self.panels[0].request("POST", f"panels/{lp['id']}/restart"))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed = any(_operation_failed(item) for item in results)
        if failed:
            logger.warning("One or more panels failed to restart")
        return not failed

    async def restart_single(self, panel_idx):
        if 0 <= panel_idx < len(self.panels):
            return await self.panels[panel_idx].request("POST", "restart")
        return None

    async def restart_linked_panel(self, panel_id):
        if self.panels:
            return await self.panels[0].request("POST", f"panels/{panel_id}/restart")
        return None

    async def get_client_stats_aggregate(self, email, inbound_tag=None):
        total_up = 0
        total_down = 0
        limit = 0
        expiry = 0
        enable = False
        found = False
        per_server = []

        for p in self.panels:
            target_inbound = self._resolve_inbound_tag(p, inbound_tag)
            inbounds = await p.request("GET", "inbounds")
            inbounds_list = _extract_inbounds(inbounds)
            server_found = False
            server_up = 0
            server_down = 0

            if inbounds_list:
                for ib in inbounds_list:
                    if ib.get("tag") != target_inbound:
                        continue

                    for c in ib.get("settings", {}).get("clients", []):
                        if c.get("email") == email:
                            found = True
                            server_found = True
                            u = c.get("up", 0)
                            d = c.get("down", 0)
                            server_up += u
                            server_down += d
                            total_up += u
                            total_down += d
                            limit = c.get("limit_bytes", 0)
                            expiry = c.get("expiry_time", 0)
                            enable = c.get("enable", True)

            if server_found:
                per_server.append(
                    {
                        "name": p.name,
                        "up": server_up,
                        "down": server_down,
                        "total": server_up + server_down,
                    }
                )

        if not found and not per_server:
            return None

        return {
            "email": email,
            "up": total_up,
            "down": total_down,
            "total": total_up + total_down,
            "limit": limit,
            "expiry": expiry,
            "enable": enable,
            "per_server": per_server,
        }

    async def _fetch_raw_subscription_pairs(self, email, inbound_tag=None):
        """Fetch subscription links from every panel.

        Returns a list of (SinglePanel, raw_link_string) pairs. The link string
        is the original text from the panel's /sub response — remarks are NOT
        rewritten here. Panels where the user does not exist are skipped silently.
        """
        pairs = []
        for p in self.panels:
            target_inbound = self._resolve_inbound_tag(p, inbound_tag)
            inbounds = await p.request("GET", "inbounds")
            inbounds_list = _extract_inbounds(inbounds)
            if not inbounds_list:
                continue

            user_uuid = None
            for ib in inbounds_list:
                if ib.get("tag") != target_inbound:
                    continue
                for c in ib.get("settings", {}).get("clients", []) or []:
                    if c.get("email") == email:
                        user_uuid = c.get("id")
                        break
                if user_uuid:
                    break

            if not user_uuid:
                continue

            raw_sub = await p.request("GET", f"sub/{quote(str(user_uuid), safe='')}")
            if not (raw_sub and isinstance(raw_sub, str)):
                continue
            try:
                decoded = base64.b64decode(raw_sub).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
                logger.warning("[%s] Invalid subscription payload: %s", p.name, exc)
                continue
            for link in decoded.split("\n"):
                link = link.strip()
                if link:
                    pairs.append((p, link))
        return pairs

    async def get_dedup_subscription_links(self, email, inbound_tag=None):
        """Fetch subscription links across all panels, dedup by (scheme, host, port).

        Dedup rule: when the same endpoint is returned by multiple panels, prefer
        the panel whose own hostname equals the link's host (the "direct" source).
        Each link's remark (text after `#`) is whatever the source panel emitted —
        backend subscription endpoint now uses Inbound.label (admin-editable
        Display Label) instead of the raw email.
        """
        pairs = await self._fetch_raw_subscription_pairs(email, inbound_tag=inbound_tag)
        deduped = _dedup_pairs(pairs)

        results = [link for _, link in deduped]
        return results

    async def get_subscription_link_single(self, email, panel_idx, inbound_tag=None):
        if not (0 <= panel_idx < len(self.panels)):
            return None

        p = self.panels[panel_idx]
        target_inbound = self._resolve_inbound_tag(p, inbound_tag)
        inbounds = await p.request("GET", "inbounds")
        inbounds_list = _extract_inbounds(inbounds)
        if not inbounds_list:
            return None

        user_uuid = None
        for ib in inbounds_list:
            if ib.get("tag") != target_inbound:
                continue

            for c in ib.get("settings", {}).get("clients", []):
                if c.get("email") == email:
                    user_uuid = c.get("id")
                    break
            if user_uuid:
                break

        if user_uuid:
            raw_sub = await p.request("GET", f"sub/{quote(str(user_uuid), safe='')}")
            if raw_sub and isinstance(raw_sub, str):
                try:
                    decoded = base64.b64decode(raw_sub).decode("utf-8")
                    links = decoded.split("\n")
                    results = []
                    for link in links:
                        if link.strip():
                            if "#" in link:
                                base, remark = link.split("#", 1)
                                new_remark = f"{p.name}-{remark}"
                                results.append(f"{base}#{new_remark}")
                            else:
                                results.append(f"{link}#{p.name}")
                    return results
                except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
                    logger.warning("[%s] Invalid subscription payload: %s", p.name, exc)
        return None


panel_api = MultiPanelManager()
