import aiohttp
import logging
import asyncio
import base64
import binascii
from urllib.parse import quote
from config import SERVERS_CONFIG

logger = logging.getLogger(__name__)


def _is_error_response(payload):
    return isinstance(payload, dict) and "error" in payload


def _extract_inbounds(payload):
    if isinstance(payload, list):
        return payload
    return None


def _operation_failed(result):
    return result is None or isinstance(result, Exception) or _is_error_response(result)


class SinglePanel:
    def __init__(self, conf):
        self.name = conf["name"]
        self.base_url = conf["url"].rstrip("/")
        self.username = conf["user"]
        self.password = conf["password"]
        self.target_inbound = conf["inbound_tag"]
        self.token = None
        self.session = None

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None
        self.token = None

    async def login(self):
        await self.ensure_session()
        try:
            async with self.session.post(
                f"{self.base_url}/api/login",
                json={"username": self.username, "password": self.password},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.token = data.get("token")
                    return True
                logger.warning(f"[{self.name}] Login failed: {resp.status}")
                return False
        except Exception as e:
            logger.error(f"[{self.name}] Login error: {e}")
            return False

    async def request(self, method, endpoint, json_data=None, params=None, data=None):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return await self._execute_request(method, endpoint, json_data, params, data)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"[{self.name}] Request {endpoint} failed (Attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                else:
                    return None
            except Exception as e:
                logger.error(f"[{self.name}] Unexpected error {endpoint}: {e}")
                return None

    async def _execute_request(self, method, endpoint, json_data, params, data):
        await self.ensure_session()
        if not self.token:
            if not await self.login():
                return {"error": "Auth Failed"}

        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.base_url}/api/{endpoint}"

        async with self.session.request(method, url, json=json_data, params=params, data=data, headers=headers) as resp:
            if resp.status == 401:
                if await self.login():
                    headers = {"Authorization": f"Bearer {self.token}"}
                    async with self.session.request(
                        method,
                        url,
                        json=json_data,
                        params=params,
                        data=data,
                        headers=headers,
                    ) as resp2:
                        return await self._process_response(resp2)
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
        self.panels = [SinglePanel(conf) for conf in SERVERS_CONFIG]

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
        report = {"success": [], "failed": []}

        base_data = {
            "email": email,
            "limit_bytes": limit_bytes,
            "expiry_time": expiry_time,
        }
        if user_id:
            base_data["id"] = user_id

        for p in self.panels:
            target_inbound = self._resolve_inbound_tag(p, inbound_tag)
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
        tasks = []
        for p in self.panels:
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
        tasks = []
        for p in self.panels:
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
        tasks = []
        for p in self.panels:
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
        return await self.panels[0].request("GET", "backup")

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
