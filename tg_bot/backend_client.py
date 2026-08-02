import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class BackendClient:
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        self._base_url = (base_url or os.environ.get("BACKEND_API_URL", "")).rstrip("/")
        self._token = token or os.environ.get("BOT_SERVICE_TOKEN", "")
        self._client: Optional[httpx.AsyncClient] = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=10.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_texts(self, lang: str) -> dict:

        c = self._ensure_client()
        resp = await c.get("/bot-service/texts", params={"lang": lang})
        resp.raise_for_status()
        return resp.json()

    async def upsert_user(
        self,
        telegram_id: int,
        username: Optional[str],
        language_code: Optional[str],
    ) -> dict:

        c = self._ensure_client()
        resp = await c.post(
            "/bot-service/users",
            json={
                "telegram_id": telegram_id,
                "username": username,
                "language_code": language_code,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_user_state(self, telegram_id: int) -> dict:

        c = self._ensure_client()
        resp = await c.get(f"/bot-service/users/{telegram_id}/state")
        resp.raise_for_status()
        return resp.json()

    async def set_language(self, telegram_id: int, language: str) -> dict:

        c = self._ensure_client()
        resp = await c.post(
            f"/bot-service/users/{telegram_id}/language",
            json={"language": language},
        )
        resp.raise_for_status()
        return resp.json()

    async def activate_trial(self, telegram_id: int) -> dict:

        c = self._ensure_client()
        resp = await c.post(
            "/bot-service/trial/activate",
            json={"telegram_id": telegram_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_tariffs(self, telegram_id: int) -> list[dict]:

        c = self._ensure_client()
        resp = await c.get("/bot-service/tariffs", params={"for": telegram_id})
        resp.raise_for_status()
        return resp.json()

    async def create_checkout(self, telegram_id: int, tariff_id: int, lang: str) -> dict:
        c = self._ensure_client()
        resp = await c.post(
            "/billing/checkout",
            json={"telegram_id": telegram_id, "tariff_id": tariff_id, "lang": lang},
        )
        resp.raise_for_status()
        return resp.json()

    async def cancel_payment(self, payment_id: int, telegram_id: int) -> dict:
        c = self._ensure_client()
        resp = await c.post(
            f"/bot-service/payments/{payment_id}/cancel",
            json={"telegram_id": telegram_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def claim_notification(
        self,
        telegram_id: int,
        kind: str,
        tariff_id: int | None,
        scope: str,
    ) -> dict:
        c = self._ensure_client()
        resp = await c.post(
            "/bot-service/notifications/claim",
            json={
                "telegram_id": telegram_id,
                "kind": kind,
                "tariff_id": tariff_id,
                "scope": scope,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def set_payment_chat_coords(
        self,
        payment_id: int,
        chat_id: int,
        message_id: int,
        telegram_id: int,
    ) -> dict:

        c = self._ensure_client()
        resp = await c.post(
            f"/bot-service/payments/{payment_id}/chat-coords",
            json={"chat_id": chat_id, "message_id": message_id, "telegram_id": telegram_id},
        )
        resp.raise_for_status()
        return resp.json()
