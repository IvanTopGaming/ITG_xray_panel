import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from backend_client import BackendClient
from i18n import I18n

logger = logging.getLogger(__name__)
_USER_CACHE_TTL = 15.0


class LangMiddleware(BaseMiddleware):
    def __init__(self, backend: BackendClient, i18n: I18n):
        super().__init__()
        self._backend = backend
        self._i18n = i18n

        self._user_cache: Dict[int, Tuple[float, str, bool, bool]] = {}
        self._lock = asyncio.Lock()

    def invalidate(self, telegram_id: int) -> None:

        self._user_cache.pop(telegram_id, None)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user: Optional[User] = data.get("event_from_user")
        lang = "ru"
        blocked = False
        language_chosen = True
        if tg_user is not None:
            lang, blocked, language_chosen = await self._resolve(tg_user)
        if blocked:
            logger.info("ignoring update from blocked user %s", tg_user and tg_user.id)
            return None
        data["lang"] = lang
        data["language_chosen"] = language_chosen
        data["i18n"] = self._i18n
        data["backend"] = self._backend
        return await handler(event, data)

    async def _resolve(self, user: User) -> Tuple[str, bool, bool]:
        cached = self._user_cache.get(user.id)
        if cached is not None and (time.time() - cached[0]) < _USER_CACHE_TTL:
            return cached[1], cached[2], cached[3]
        try:
            response = await self._backend.upsert_user(
                telegram_id=user.id,
                username=user.username,
                language_code=user.language_code,
            )
            lang = response.get("language", "ru")
            blocked = bool(response.get("blocked", False))

            language_chosen = bool(response.get("language_chosen", True))
        except Exception as exc:
            logger.info("upsert_user failed for %s: %s", user.id, exc)
            code = (user.language_code or "ru").lower()
            lang = "ru" if code.startswith("ru") else "en"
            blocked = False

            language_chosen = True
        async with self._lock:
            self._user_cache[user.id] = (time.time(), lang, blocked, language_chosen)
        return lang, blocked, language_chosen
