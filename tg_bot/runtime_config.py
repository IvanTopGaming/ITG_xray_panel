from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable, Optional

import httpx

from version import get_bot_version

logger = logging.getLogger(__name__)


class RuntimeConfig:
    REFRESH_INTERVAL_SECONDS = 30
    BOOTSTRAP_RETRY_SECONDS = 5

    def __init__(self) -> None:
        self.version: int = 0
        self.bot_token: str = ""
        self.admin_ids: list[int] = []
        self._admin_ids_set: set[int] = set()
        self.telegram_proxy_url: str = ""
        self.display_timezone: str = "Europe/Moscow"

        self._backend_url = (os.environ.get("BACKEND_API_URL") or "").rstrip("/")
        self._service_token = os.environ.get("BOT_SERVICE_TOKEN") or ""
        self._client: Optional[httpx.AsyncClient] = None
        self._session_change_listener: Optional[Callable[[], Awaitable[None]]] = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self._backend_url or not self._service_token:
                raise RuntimeError("BACKEND_API_URL and BOT_SERVICE_TOKEN env vars are required")
            self._client = httpx.AsyncClient(
                base_url=self._backend_url,
                headers={
                    "Authorization": f"Bearer {self._service_token}",
                    "X-Bot-Version": get_bot_version(),
                },
                timeout=10.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _fetch(self) -> dict:
        client = self._ensure_client()
        resp = await client.get("/bot/runtime-config")
        resp.raise_for_status()
        return resp.json()

    def admin_ids_set(self) -> set[int]:
        return self._admin_ids_set

    def set_change_listener(self, listener: Callable[[bool], Awaitable[None]]) -> None:

        self._session_change_listener = listener

    def _apply(self, payload: dict) -> bool:

        new_token = str(payload.get("bot_token") or "")
        new_proxy = str(payload.get("telegram_proxy_url") or "")
        session_affecting = new_token != self.bot_token or new_proxy != self.telegram_proxy_url

        self.version = int(payload.get("version") or 0)
        self.bot_token = new_token
        self.telegram_proxy_url = new_proxy
        raw_admins = payload.get("admin_ids") or []
        cleaned: list[int] = []
        for item in raw_admins:
            try:
                cleaned.append(int(item))
            except (TypeError, ValueError):
                continue
        self.admin_ids = cleaned
        self._admin_ids_set = set(cleaned)
        self.display_timezone = str(payload.get("display_timezone") or "Europe/Moscow")
        return session_affecting

    async def bootstrap(self) -> None:

        logger.info("runtime_config: bootstrap from %s", self._backend_url)
        while True:
            try:
                payload = await self._fetch()
            except Exception as exc:
                logger.info(
                    "runtime_config: bootstrap fetch failed: %s — retry in %ss",
                    exc,
                    self.BOOTSTRAP_RETRY_SECONDS,
                )
                await asyncio.sleep(self.BOOTSTRAP_RETRY_SECONDS)
                continue
            self._apply(payload)
            if self.bot_token:
                logger.info(
                    "runtime_config: ready (version=%s, %d admin(s))",
                    self.version,
                    len(self.admin_ids),
                )
                return
            logger.info(
                "runtime_config: bot_token not yet set in panel — waiting (retry in %ss)",
                self.BOOTSTRAP_RETRY_SECONDS,
            )
            await asyncio.sleep(self.BOOTSTRAP_RETRY_SECONDS)

    async def refresh_loop(self) -> None:

        while True:
            await asyncio.sleep(self.REFRESH_INTERVAL_SECONDS)
            try:
                payload = await self._fetch()
            except Exception as exc:
                logger.info("runtime_config: refresh failed: %s", exc)
                continue
            if int(payload.get("version") or 0) == self.version:
                continue
            old_version = self.version
            session_changed = self._apply(payload)
            logger.info(
                "runtime_config: version %s → %s%s",
                old_version,
                self.version,
                " (session restart needed)" if session_changed else "",
            )
            if self._session_change_listener is not None:
                try:
                    await self._session_change_listener(session_changed)
                except Exception as exc:
                    logger.exception("runtime_config: change listener failed: %s", exc)


runtime_config = RuntimeConfig()
