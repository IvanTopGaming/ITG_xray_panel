import aiohttp
import logging
import asyncio
import base64
import binascii
import json
import os
import re
import time
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


def _looks_masked(value):
    """Heuristic: detect placeholder passwords the panel returns when masking."""
    if not isinstance(value, str) or not value:
        return True
    return set(value) <= {"•", "*", "X", "x", "·"}


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
    def __init__(self, conf, *, is_virtual=False, parent_master=None):
        self.name = conf["name"]
        self.base_url = conf["url"].rstrip("/")
        self.token = conf["token"]
        self.target_inbound = conf["inbound_tag"]
        self.role = conf.get("role", "standalone")
        self.is_virtual = is_virtual
        self.parent_master = parent_master
        self.session = None

    @property
    def is_master(self):
        return self.role == "master"

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
                logger.warning(f"[{self.name}] Request {endpoint} failed (Attempt {attempt + 1}/{max_retries}): {err}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                else:
                    return None
            except Exception as e:
                logger.error(f"[{self.name}] Unexpected error {endpoint}: {e}")
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
                logger.warning(f"[{self.name}] bot_service_token rejected by panel")
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
    SHADOW_CACHE_TTL = 300  # seconds

    def __init__(self):
        # Single-master topology: panels[] always has zero or one entry,
        # rebuilt from runtime_config (panel admin user/password + the
        # backend URL the bot is configured to talk to). Filled in on
        # first reload_from_runtime() call.
        self.panels: list[SinglePanel] = []
        self._shadow_cache = None
        self._shadow_cache_expires = 0.0
        self._shadow_cache_lock = asyncio.Lock()

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

    async def _fetch_master_node_hosts(self, master_panel):
        """Return the set of lowercased hostnames of a master's sync-enabled nodes."""
        hosts = set()
        try:
            nodes = await master_panel.request("GET", "nodes")
        except Exception as exc:
            logger.warning(
                "[%s] Failed to fetch node list for shadow detection: %s",
                master_panel.name,
                exc,
            )
            return hosts

        if not isinstance(nodes, list):
            return hosts

        for n in nodes:
            if not isinstance(n, dict):
                continue
            if not n.get("enable", True):
                continue
            if not n.get("sync_users", False):
                continue
            url = n.get("url") or ""
            if not url:
                continue
            try:
                parsed = urlparse(url)
            except ValueError:
                continue
            host = (parsed.hostname or "").strip().lower()
            if host:
                hosts.add(host)
        return hosts

    async def _resolve_shadow_map(self):
        """Return mapping {shadowed_panel_name: master_panel_name}.

        A panel is "shadowed" when its hostname matches one of a master panel's
        sync-enabled node hostnames. Shadowed panels are skipped in write
        operations because the master already fans the change out to them.
        The value is the name of the master that owns that node.
        Result is cached for SHADOW_CACHE_TTL seconds.
        """
        now = time.time()
        if self._shadow_cache is not None and now < self._shadow_cache_expires:
            return self._shadow_cache

        async with self._shadow_cache_lock:
            now = time.time()
            if self._shadow_cache is not None and now < self._shadow_cache_expires:
                return self._shadow_cache

            master_panels = [p for p in self.panels if p.is_master]
            if not master_panels:
                self._shadow_cache = {}
                self._shadow_cache_expires = now + self.SHADOW_CACHE_TTL
                return self._shadow_cache

            node_host_results = await asyncio.gather(
                *[self._fetch_master_node_hosts(m) for m in master_panels],
                return_exceptions=False,
            )

            shadowed = {}
            for p in self.panels:
                if p.is_master:
                    continue
                ph = _panel_hostname(p)
                if not ph:
                    continue
                for master, hosts in zip(master_panels, node_host_results):
                    if ph in hosts:
                        shadowed[p.name] = master.name
                        break

            self._shadow_cache = shadowed
            self._shadow_cache_expires = now + self.SHADOW_CACHE_TTL
            return shadowed

    def invalidate_shadow_cache(self):
        self._shadow_cache = None
        self._shadow_cache_expires = 0.0

    async def writable_panels(self):
        shadowed = await self._resolve_shadow_map()
        return [p for p in self.panels if not p.is_virtual and p.name not in shadowed]

    async def _fetch_master_nodes_full(self, master_panel):
        """Return list of node dicts with unmasked credentials, or []."""
        try:
            nodes = await master_panel.request("GET", "nodes", params={"include_password": "1"})
        except Exception as exc:
            logger.warning("[%s] Failed to fetch nodes with credentials: %s", master_panel.name, exc)
            return []
        if not isinstance(nodes, list):
            return []
        return nodes

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

    async def get_system_stats_all(self):
        stats_list = []
        for p in self.panels:
            res = await p.request("GET", "stats/system")
            if isinstance(res, dict) and "error" not in res:
                res["server_name"] = p.name
                stats_list.append(res)
            else:
                stats_list.append({"server_name": p.name, "error": True})
        return stats_list

    async def restart_all(self):
        tasks = [p.request("POST", "restart") for p in self.panels]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed = any(_operation_failed(item) for item in results)
        if failed:
            logger.warning("One or more panels failed to restart")
        return not failed

    async def restart_single(self, panel_idx):
        if 0 <= panel_idx < len(self.panels):
            return await self.panels[panel_idx].request("POST", "restart")
        return None

    async def add_client_all(self, email, limit_bytes=0, expiry_time=0, user_id=None, inbound_tag=None):
        report = {"success": [], "failed": [], "skipped": []}

        base_data = {
            "email": email,
            "limit_bytes": limit_bytes,
            "expiry_time": expiry_time,
        }
        if user_id:
            base_data["id"] = user_id

        shadowed = await self._resolve_shadow_map()
        for p in self.panels:
            target_inbound = self._resolve_inbound_tag(p, inbound_tag)
            if p.is_virtual:
                master_name = p.parent_master or "master"
                report["skipped"].append(
                    {
                        "name": p.name,
                        "reason": f"managed by {master_name}",
                        "inbound_tag": target_inbound,
                    }
                )
                continue
            if p.name in shadowed:
                report["skipped"].append(
                    {
                        "name": p.name,
                        "reason": f"managed by {shadowed[p.name]}",
                        "inbound_tag": target_inbound,
                    }
                )
                continue

            res = await p.request(
                "POST",
                f"inbounds/{target_inbound}/users",
                json_data=base_data,
            )

            if isinstance(res, dict) and "id" in res:
                report["success"].append({"name": p.name, "uuid": res["id"], "inbound_tag": target_inbound})
            else:
                reason = "Unknown Error"
                if isinstance(res, dict) and "error" in res:
                    if "Inbound not found" in str(res["error"]) or res.get("status") == 404:
                        reason = f"Inbound '{target_inbound}' not found"
                    elif "Email exists" in str(res["error"]):
                        reason = "User already exists"
                    else:
                        reason = str(res["error"])
                elif res is None:
                    reason = "Connection Timeout"

                report["failed"].append({"name": p.name, "reason": reason, "inbound_tag": target_inbound})

        return report

    async def add_client_single(
        self,
        server_name,
        email,
        limit_bytes=0,
        expiry_time=0,
        user_id=None,
        inbound_tag=None,
    ):
        target_panel = next((p for p in self.panels if p.name == server_name), None)
        if not target_panel:
            return {"error": "Server not found in config"}
        target_inbound = self._resolve_inbound_tag(target_panel, inbound_tag)

        data = {"email": email, "limit_bytes": limit_bytes, "expiry_time": expiry_time}
        if user_id:
            data["id"] = user_id

        res = await target_panel.request("POST", f"inbounds/{target_inbound}/users", json_data=data)

        if isinstance(res, dict) and "id" in res:
            return {"success": True, "uuid": res["id"]}

        reason = "Unknown Error"
        if isinstance(res, dict) and "error" in res:
            reason = str(res["error"])
        elif res is None:
            reason = "Connection Timeout"

        return {"success": False, "error": reason}

    async def update_client_all(self, email, updates, inbound_tag=None):
        writable = await self.writable_panels()
        tasks = []
        for p in writable:
            target_inbound = self._resolve_inbound_tag(p, inbound_tag)
            data = updates.copy()
            data["tag"] = target_inbound
            data["old_email"] = email
            tasks.append(p.request("PUT", f"inbounds/{target_inbound}/users", json_data=data))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed = any(_operation_failed(item) for item in results)
        if failed:
            logger.warning("One or more panels failed to update user %s", email)
        return not failed

    async def delete_client_all(self, email, inbound_tag=None):
        writable = await self.writable_panels()
        tasks = []
        for p in writable:
            target_inbound = self._resolve_inbound_tag(p, inbound_tag)
            tasks.append(
                p.request(
                    "DELETE",
                    f"inbounds/{target_inbound}/users",
                    params={"email": email},
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed = any(_operation_failed(item) for item in results)
        if failed:
            logger.warning("One or more panels failed to delete user %s", email)
        return not failed

    async def reset_traffic_all(self, email, inbound_tag=None):
        writable = await self.writable_panels()
        tasks = []
        for p in writable:
            target_inbound = self._resolve_inbound_tag(p, inbound_tag)
            tasks.append(
                p.request(
                    "POST",
                    "users/reset-traffic",
                    json_data={"tag": target_inbound, "email": email},
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed = any(_operation_failed(item) for item in results)
        if failed:
            logger.warning("One or more panels failed to reset traffic for %s", email)
        return not failed

    async def get_client_stats_single(self, panel_idx, email, inbound_tag=None):
        if not (0 <= panel_idx < len(self.panels)):
            return None

        p = self.panels[panel_idx]
        target_inbound = self._resolve_inbound_tag(p, inbound_tag)
        inbounds = await p.request("GET", "inbounds")
        inbounds_list = _extract_inbounds(inbounds)

        if not inbounds_list:
            return {"name": p.name, "error": True}

        for ib in inbounds_list:
            if ib.get("tag") != target_inbound:
                continue

            for c in ib.get("settings", {}).get("clients", []):
                if c.get("email") == email:
                    return {
                        "name": p.name,
                        "up": c.get("up", 0),
                        "down": c.get("down", 0),
                        "limit": c.get("limit_bytes", 0),
                        "expiry": c.get("expiry_time", 0),
                        "enable": c.get("enable", True),
                        "error": False,
                    }

        return {"name": p.name, "error": True, "reason": "Not Found"}

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

    async def get_all_subscription_links(self, email, inbound_tag=None):
        links = []
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
                        for link in decoded.split("\n"):
                            if link.strip():
                                if "#" in link:
                                    base, remark = link.split("#", 1)
                                    new_remark = f"{p.name}-{remark}"
                                    links.append(f"{base}#{new_remark}")
                                else:
                                    links.append(f"{link}#{p.name}")
                    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
                        logger.warning("[%s] Invalid subscription payload: %s", p.name, exc)
        return links

    async def download_backup_first(self):
        if not self.panels:
            return None
        return await self.panels[0].request("GET", "backup", timeout=300)

    async def restore_backup(self, file_bytes, panel_idx):
        if not (0 <= panel_idx < len(self.panels)):
            return None
        import aiohttp

        form = aiohttp.FormData()
        form.add_field(
            "file",
            file_bytes,
            filename="restore.db",
            content_type="application/x-sqlite3",
        )
        return await self.panels[panel_idx].request("POST", "restore", data=form)


panel_api = MultiPanelManager()
