"""Redis subscriber: backend → bot push events."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from typing import Any, Awaitable, Callable, Optional, Union

import redis.asyncio as redis_async
from aiogram import Bot
from zoneinfo import ZoneInfo

import keyboards as kb
from i18n import I18n
from runtime_config import runtime_config

logger = logging.getLogger(__name__)
_CHANNEL = "bot:events"

# Bot or callable returning one — accessor pattern so hot-swap can replace Bot under us.
BotSource = Union[Bot, Callable[[], Union[Bot, Awaitable[Bot]]]]


async def _resolve_bot(source: BotSource) -> Bot:
    if callable(source):
        result = source()
        if asyncio.iscoroutine(result):
            result = await result
        return result  # type: ignore[return-value]
    return source


def _redis_uri() -> Optional[str]:
    raw = (os.getenv("RATELIMIT_STORAGE_URI") or "").strip()
    if raw.startswith("redis://"):
        return raw
    return None


def _format_expires_at(expires_at_ms: Optional[int]) -> str:
    if not expires_at_ms:
        return "?"
    try:
        tz = ZoneInfo(runtime_config.display_timezone or "Europe/Moscow")
    except Exception:
        tz = ZoneInfo("UTC")
    d = dt.datetime.fromtimestamp(expires_at_ms / 1000, tz=tz)
    return d.strftime("%d.%m.%Y %H:%M")


async def _handle(event: dict[str, Any], bot_source: BotSource, i18n: I18n, middleware) -> None:
    bot = await _resolve_bot(bot_source)
    etype = event.get("type")
    tg_id = event.get("telegram_id")
    payload = event.get("payload") or {}
    lang = payload.get("lang", "ru")

    if etype == "texts_changed":
        target_lang = payload.get("lang")
        await i18n.invalidate(target_lang)
        return

    if tg_id is None:
        return

    # Block/unblock/language-change: drop the cached entry so the next event
    # from this user picks up the new state immediately, not after the 15 s TTL.
    if etype in ("user_blocked", "user_unblocked", "user_language_changed") and middleware is not None:
        try:
            middleware.invalidate(int(tg_id))
        except Exception as exc:
            logger.info("middleware.invalidate %s failed: %s", tg_id, exc)
        return

    markup = None
    if etype == "payment_succeeded":
        text = await i18n.t(
            "notification.payment_succeeded",
            lang,
            expires=_format_expires_at(payload.get("expires_at_ms")),
        )
        subs_label = await i18n.t("menu.subscription", lang)
        back_label = await i18n.t("common.back_to_main", lang)
        markup = kb.trial_success_kb(subs_label=subs_label, back_label=back_label)
    elif etype == "payment_cancelled":
        text = await i18n.t("notification.payment_cancelled", lang)
        tariffs_label = await i18n.t("menu.tariffs", lang)
        back_label = await i18n.t("common.back_to_main", lang)
        markup = kb.payment_retry_kb(tariffs_label=tariffs_label, back_label=back_label)
    elif etype == "payment_failed":
        text = await i18n.t("notification.payment_failed", lang)
        tariffs_label = await i18n.t("menu.tariffs", lang)
        back_label = await i18n.t("common.back_to_main", lang)
        markup = kb.payment_retry_kb(tariffs_label=tariffs_label, back_label=back_label)
    elif etype == "access_renewed":
        text = await i18n.t("notification.access_renewed", lang)
    elif etype == "access_paused":
        text = await i18n.t(
            "notification.access_paused",
            lang,
            tariff_name=payload.get("tariff_name", ""),
        )
        subs_label = await i18n.t("menu.subscription", lang)
        back_label = await i18n.t("common.back_to_main", lang)
        markup = kb.trial_success_kb(subs_label=subs_label, back_label=back_label)
    elif etype == "access_granted":
        text = await i18n.t(
            "notification.access_granted",
            lang,
            tariff_name=payload.get("tariff_name", ""),
            expires=_format_expires_at(payload.get("expires_at_ms")),
        )
        subs_label = await i18n.t("menu.subscription", lang)
        back_label = await i18n.t("common.back_to_main", lang)
        markup = kb.trial_success_kb(subs_label=subs_label, back_label=back_label)
    elif etype == "access_granted_once":
        text = await i18n.t(
            "notification.access_granted_once",
            lang,
            tariff_name=payload.get("tariff_name", ""),
            expires=_format_expires_at(payload.get("expires_at_ms")),
        )
        subs_label = await i18n.t("menu.subscription", lang)
        back_label = await i18n.t("common.back_to_main", lang)
        markup = kb.trial_success_kb(subs_label=subs_label, back_label=back_label)
    elif etype == "access_offered":
        text = await i18n.t(
            "notification.access_offered",
            lang,
            tariff_name=payload.get("tariff_name", ""),
        )
        tariffs_label = await i18n.t("menu.tariffs", lang)
        back_label = await i18n.t("common.back_to_main", lang)
        markup = kb.payment_retry_kb(tariffs_label=tariffs_label, back_label=back_label)
    elif etype == "expiry_notification":
        from aiogram import types as _types

        kind = payload.get("kind", "expired")
        key = {
            "expiry_3d": "notification.expiry_3d",
            "expiry_1d": "notification.expiry_1d",
            "expiry_1h": "notification.expiry_1h",
            "expired": "notification.expired",
        }.get(kind, "notification.expired")
        text = await i18n.t(
            key,
            lang,
            email=payload.get("email", ""),
            expires=_format_expires_at(payload.get("expiry_time_ms")),
        )
        # Renew button shows only when the backend has pre-confirmed the tariff
        # is still purchasable (not archived, not disabled, not trial, and for
        # private — user has a grant). Otherwise we'd send the user to a button
        # that just rejects with "tariff_not_available".
        rows = []
        tariff_id = payload.get("tariff_id")
        if payload.get("renewable") and tariff_id:
            renew_label = await i18n.t("notification.button.renew", lang)
            rows.append([_types.InlineKeyboardButton(text=renew_label, callback_data=f"buy:{tariff_id}")])
        home_label = await i18n.t("common.back_to_main", lang)
        rows.append([_types.InlineKeyboardButton(text=home_label, callback_data="user_home")])
        keyboard = _types.InlineKeyboardMarkup(inline_keyboard=rows)
        try:
            await bot.send_message(tg_id, text, reply_markup=keyboard)
        except Exception as exc:
            logger.error("bot_events.send to %s failed: %s", tg_id, exc)
        return
    elif etype == "traffic_notification":
        from aiogram import types as _types

        from utils import format_bytes

        kind = payload.get("kind", "traffic_exhausted")
        key = {
            "traffic_80": "notification.traffic_80",
            "traffic_95": "notification.traffic_95",
            "traffic_exhausted": "notification.traffic_exhausted",
        }.get(kind, "notification.traffic_exhausted")
        used = int(payload.get("used_bytes") or 0)
        limit = int(payload.get("limit_bytes") or 0)
        remaining = max(0, limit - used)
        text = await i18n.t(
            key,
            lang,
            email=payload.get("email", ""),
            used=format_bytes(used),
            limit=format_bytes(limit),
            remaining=format_bytes(remaining),
        )
        rows = []
        tariff_id = payload.get("tariff_id")
        if payload.get("renewable") and tariff_id:
            renew_label = await i18n.t("notification.button.renew", lang)
            rows.append([_types.InlineKeyboardButton(text=renew_label, callback_data=f"buy:{tariff_id}")])
        home_label = await i18n.t("common.back_to_main", lang)
        rows.append([_types.InlineKeyboardButton(text=home_label, callback_data="user_home")])
        keyboard = _types.InlineKeyboardMarkup(inline_keyboard=rows)
        try:
            await bot.send_message(tg_id, text, reply_markup=keyboard)
        except Exception as exc:
            logger.error("bot_events.send to %s failed: %s", tg_id, exc)
        return
    else:
        return

    chat_id = payload.get("chat_id")
    message_id = payload.get("message_id")
    if chat_id and message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            logger.info("delete_message failed for %s (%s/%s): %s", etype, chat_id, message_id, exc)
    try:
        await bot.send_message(tg_id, text, reply_markup=markup, parse_mode="HTML")
    except Exception as exc:
        logger.warning("bot_events.send to %s failed: %s", tg_id, exc)


async def run_consumer(bot_source: BotSource, i18n: I18n, middleware=None) -> None:
    uri = _redis_uri()
    if uri is None:
        logger.warning("bot_events_consumer: no redis URI; events disabled")
        return

    backoff = 1.0
    while True:
        try:
            client = redis_async.from_url(
                uri,
                socket_keepalive=True,
                health_check_interval=30,
            )
            pubsub = client.pubsub()
            await pubsub.subscribe(_CHANNEL)
            logger.info("bot_events_consumer: subscribed to %s", _CHANNEL)
            backoff = 1.0
            while True:
                raw = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=30.0,
                )
                if raw is None:
                    continue
                if raw.get("type") != "message":
                    continue
                try:
                    event = json.loads(raw["data"])
                except Exception:
                    continue
                await _handle(event, bot_source, i18n, middleware)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("bot_events_consumer: %s — reconnecting in %.1fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
