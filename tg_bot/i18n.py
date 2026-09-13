import asyncio
import logging
import time
from string import Formatter
from typing import Dict, Optional

from backend_client import BackendClient

logger = logging.getLogger(__name__)
_TTL_SECONDS = 60
_FAILURE_TTL_SECONDS = 10
_FALLBACK = {
    "ru": {
        "errors.service_unavailable": "Сервис временно недоступен. Попробуй ещё раз немного позже.",
        "common.back_to_main": "Главное меню",
        "catalog.invoice_closed.message": "Счёт скрыт. Если у тебя осталась страница оплаты, она может ещё принимать оплату.",
        "keys.list.entry": "🔑 {name}",
        "trial.pending": "Активация ещё не завершена. Попробуй снова немного позже — повторная попытка продолжит текущую заявку.",
    },
    "en": {
        "errors.service_unavailable": "Service temporarily unavailable. Please try again shortly.",
        "common.back_to_main": "Main menu",
        "catalog.invoice_closed.message": "Invoice hidden. A payment page you already opened may still accept payment.",
        "keys.list.entry": "🔑 {name}",
        "trial.pending": "Activation is still pending. Please try again shortly — your next attempt will resume this request.",
    },
}


def format_text(text: str, values: dict) -> str:
    for _, field, spec, conversion in Formatter().parse(text):
        if field is not None and (not field.isidentifier() or spec or conversion):
            raise ValueError("unsupported format field")
    return text.format(**values) if values else text


class I18n:
    def __init__(self, backend: BackendClient):
        self._backend = backend
        self._cache: Dict[str, Dict[str, str]] = {}
        self._loaded_at: Dict[str, float] = {}
        self._retry_at: Dict[str, float] = {}
        self._valid_texts: Dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self, lang: str) -> None:
        if time.monotonic() < self._retry_at.get(lang, 0):
            return
        loaded = self._loaded_at.get(lang, 0.0)
        if self._cache.get(lang) and (time.time() - loaded) < _TTL_SECONDS:
            return
        async with self._lock:
            if time.monotonic() < self._retry_at.get(lang, 0):
                return
            loaded = self._loaded_at.get(lang, 0.0)
            if self._cache.get(lang) and (time.time() - loaded) < _TTL_SECONDS:
                return
            try:
                data = await self._backend.get_texts(lang)
                self._cache[lang] = data.get("texts", {})
                self._loaded_at[lang] = time.time()
            except Exception as exc:
                self._retry_at[lang] = time.monotonic() + _FAILURE_TTL_SECONDS
                logger.warning("i18n.fetch failed for lang=%s: %s", lang, type(exc).__name__)

    async def t(self, key: str, lang: str = "ru", **vars: object) -> str:
        await self._ensure_loaded(lang)
        text: Optional[str] = self._cache.get(lang, {}).get(key)
        if text is None:
            other = "en" if lang == "ru" else "ru"
            await self._ensure_loaded(other)
            text = self._cache.get(other, {}).get(key)
        if text is None:
            text = self._fallback(key, lang)
        try:
            rendered = format_text(text, vars)
            self._valid_texts[(lang, key)] = text
            return rendered
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("i18n.format failed for key=%s lang=%s: %s", key, lang, type(exc).__name__)
            fallback = self._valid_texts.get((lang, key), self._fallback(key, lang))
            try:
                return format_text(fallback, vars)
            except (KeyError, IndexError, ValueError, TypeError, AttributeError):
                return self._fallback("errors.service_unavailable", lang)

    def _fallback(self, key: str, lang: str) -> str:
        texts = _FALLBACK.get(lang, _FALLBACK["en"])
        return texts.get(key, texts["errors.service_unavailable"])

    async def invalidate(self, lang: Optional[str] = None) -> None:
        async with self._lock:
            if lang is None:
                self._cache.clear()
                self._loaded_at.clear()
                self._retry_at.clear()
            else:
                self._cache.pop(lang, None)
                self._loaded_at.pop(lang, None)
                self._retry_at.pop(lang, None)
        logger.info("i18n: invalidated lang=%s", lang or "all")
