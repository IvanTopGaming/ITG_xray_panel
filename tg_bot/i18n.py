"""Bot i18n: fetches text strings from the backend, caches them in memory.

Phase 2 uses a 60-second TTL for cache invalidation. Phase 4 will add a
Redis subscriber that invalidates the cache instantly when admin saves.
"""

import asyncio
import logging
import time
from typing import Dict, Optional

from backend_client import BackendClient

logger = logging.getLogger(__name__)
_TTL_SECONDS = 60


class I18n:
    def __init__(self, backend: BackendClient):
        self._backend = backend
        self._cache: Dict[str, Dict[str, str]] = {}  # lang -> {key: text}
        self._loaded_at: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self, lang: str) -> None:
        loaded = self._loaded_at.get(lang, 0.0)
        if self._cache.get(lang) and (time.time() - loaded) < _TTL_SECONDS:
            return
        async with self._lock:
            # Double-check after lock
            loaded = self._loaded_at.get(lang, 0.0)
            if self._cache.get(lang) and (time.time() - loaded) < _TTL_SECONDS:
                return
            try:
                data = await self._backend.get_texts(lang)
                self._cache[lang] = data.get("texts", {})
                self._loaded_at[lang] = time.time()
            except Exception as exc:
                logger.warning("i18n.fetch failed for lang=%s: %s", lang, exc)
                # Don't bump _loaded_at so we'll retry next call.

    async def t(self, key: str, lang: str = "ru", **vars: object) -> str:
        await self._ensure_loaded(lang)
        text: Optional[str] = self._cache.get(lang, {}).get(key)
        if text is None:
            # Fallback: try the other lang
            other = "en" if lang == "ru" else "ru"
            await self._ensure_loaded(other)
            text = self._cache.get(other, {}).get(key)
        if text is None:
            return f"⟨{key}⟩"
        try:
            return text.format(**vars) if vars else text
        except (KeyError, IndexError) as exc:
            logger.warning("i18n.format failed for key=%s lang=%s: %s", key, lang, exc)
            return text

    async def invalidate(self, lang: Optional[str] = None) -> None:
        async with self._lock:
            if lang is None:
                self._cache.clear()
                self._loaded_at.clear()
            else:
                self._cache.pop(lang, None)
                self._loaded_at.pop(lang, None)
        logger.info("i18n: invalidated lang=%s", lang or "all")
