"""HTTP client for talking to the panel backend's /api/bot-service/* endpoints.

Authentication: a service token shared with the backend, set in the bot's
container environment as BACKEND_API_URL + BOT_SERVICE_TOKEN.
"""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class BackendClient:
    """Async HTTP client; one instance per bot process.

    The client is configured from environment:
      BACKEND_API_URL — e.g. http://backend:5000/api  (no trailing slash)
      BOT_SERVICE_TOKEN — the service token (matches SystemSetting on backend)
    """

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
        """Returns {"version": int, "texts": {key: text, ...}}."""
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
        """Returns the upserted TelegramUser dict including stored language."""
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
        """Returns the bot's "what to render" state for /start branching:
        {telegram_id, language, trial_available, trial_used_at, blocked,
         clients: [...], expires_at_ms}"""
        c = self._ensure_client()
        resp = await c.get(f"/bot-service/users/{telegram_id}/state")
        resp.raise_for_status()
        return resp.json()

    async def set_language(self, telegram_id: int, language: str) -> dict:
        """PATCH the user's language and mark language_chosen=true.

        Returns {telegram_id, language, language_chosen}. Raises
        httpx.HTTPStatusError on 4xx/5xx.
        """
        c = self._ensure_client()
        resp = await c.post(
            f"/bot-service/users/{telegram_id}/language",
            json={"language": language},
        )
        resp.raise_for_status()
        return resp.json()

    async def activate_trial(self, telegram_id: int) -> dict:
        """Returns {clients, expires_at_ms, source}. Raises httpx.HTTPStatusError
        on 409 (trial already used) or 404 (no trial tariff configured)."""
        c = self._ensure_client()
        resp = await c.post(
            "/bot-service/trial/activate",
            json={"telegram_id": telegram_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_tariffs(self, telegram_id: int) -> list[dict]:
        """Returns enabled, non-archived, non-trial tariffs visible to this user
        (public + private grants), sorted by sort_order."""
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

    async def get_payment(self, payment_id: int) -> dict:
        c = self._ensure_client()
        resp = await c.get(f"/bot-service/payments/{payment_id}")
        resp.raise_for_status()
        return resp.json()

    async def cancel_payment(self, payment_id: int) -> dict:
        c = self._ensure_client()
        resp = await c.post(f"/bot-service/payments/{payment_id}/cancel")
        resp.raise_for_status()
        return resp.json()

    async def set_payment_chat_coords(
        self,
        payment_id: int,
        chat_id: int,
        message_id: int,
    ) -> dict:
        """Store checkout-bubble coords so the YooKassa webhook can edit the original message in place."""
        c = self._ensure_client()
        resp = await c.post(
            f"/bot-service/payments/{payment_id}/chat-coords",
            json={"chat_id": chat_id, "message_id": message_id},
        )
        resp.raise_for_status()
        return resp.json()
