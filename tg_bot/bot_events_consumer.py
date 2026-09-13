from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from html import escape
from typing import Any, Awaitable, Callable, Optional, Union

import redis.asyncio as redis_async
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from zoneinfo import ZoneInfo

import keyboards as kb
from i18n import I18n
from runtime_config import runtime_config

logger = logging.getLogger(__name__)
_CHANNEL = "bot:events"


def h(value):
    return escape(str(value), quote=True)


BotSource = Union[Bot, Callable[[], Union[Bot, Awaitable[Bot]]]]


async def _resolve_bot(source: BotSource) -> Bot:
    if callable(source):
        result = source()
        if asyncio.iscoroutine(result):
            result = await result
        return result  # type: ignore[return-value]
    return source


def _redis_uri() -> Optional[str]:
    raw = (os.getenv("SHARED_REDIS_URI") or "").strip()
    if raw.startswith(("redis://", "rediss://")):
        return raw
    return None


async def _format_expires_at(expires_at_ms: Optional[int], *, i18n: I18n, lang: str) -> str:
    if expires_at_ms is None:
        return "?"
    try:
        value = int(expires_at_ms)
    except (TypeError, ValueError):
        return "?"
    if value <= 0:
        return await i18n.t("stats.expiry.permanent", lang)
    try:
        tz = ZoneInfo(runtime_config.display_timezone or "Europe/Moscow")
    except Exception:
        tz = ZoneInfo("UTC")
    d = dt.datetime.fromtimestamp(value / 1000, tz=tz)
    return d.strftime("%d.%m.%Y %H:%M")


async def _handle(event: dict[str, Any], bot_source: BotSource, i18n: I18n, middleware, backend=None) -> None:
    if backend is None:
        logger.error("bot event delivery requires a backend client")
        return
    try:
        verdict = await backend.claim_event(event)
    except Exception as exc:
        logger.warning("durable event claim unavailable: %s", type(exc).__name__)
        return
    if not verdict.get("claimed"):
        return
    canonical = verdict["event"]
    rendered = {
        **canonical,
        "payload": {**canonical["payload"], "lang": verdict["lang"], "renewable": verdict["renewable"]},
    }
    if canonical["type"] == "texts_changed":
        rendered = canonical
    outcome = "delivered"
    try:
        await asyncio.wait_for(_render_event(rendered, bot_source, i18n, middleware), timeout=45)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        outcome = "permanent_failure"
        logger.warning("Telegram rejected delivery: %s", type(exc).__name__)
    except Exception as exc:
        outcome = "retry"
        logger.warning("Telegram delivery will retry: %s", type(exc).__name__)
    for attempt in range(3):
        try:
            ack = await backend.ack_event(canonical["source"], canonical["id"], verdict["lease_token"], outcome)
            if ack.get("acked"):
                return
        except Exception as exc:
            logger.warning("delivery acknowledgment failed: %s", type(exc).__name__)
        await asyncio.sleep(0.5 * (2**attempt))
    logger.error("delivery acknowledgment uncertain; a later retry may repeat a Telegram message")


async def _render_event(event: dict[str, Any], bot_source: BotSource, i18n: I18n, middleware) -> None:
    bot = await _resolve_bot(bot_source)
    etype = event.get("type")
    tg_id = event.get("telegram_id")
    payload = event.get("payload") or {}
    lang = payload.get("lang", "ru")

    renewable = bool(payload.get("renewable"))

    if etype == "texts_changed":
        target_lang = payload.get("lang")
        await i18n.invalidate(target_lang)
        return

    if tg_id is None:
        return

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
            expires=await _format_expires_at(payload.get("expires_at_ms"), i18n=i18n, lang=lang),
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
    elif etype == "payment_refunded":
        text = await i18n.t("notification.payment_refunded", lang)
        tariffs_label = await i18n.t("menu.tariffs", lang)
        back_label = await i18n.t("common.back_to_main", lang)
        markup = kb.payment_retry_kb(tariffs_label=tariffs_label, back_label=back_label)
    elif etype == "sub_link_reset":
        text = await i18n.t("notification.sub_link_reset", lang)
        subs_label = await i18n.t("menu.subscription", lang)
        back_label = await i18n.t("common.back_to_main", lang)
        markup = kb.trial_success_kb(subs_label=subs_label, back_label=back_label)
    elif etype == "access_granted":
        text = await i18n.t(
            "notification.access_granted",
            lang,
            tariff_name=h(payload.get("tariff_name", "")),
            expires=await _format_expires_at(payload.get("expires_at_ms"), i18n=i18n, lang=lang),
        )
        subs_label = await i18n.t("menu.subscription", lang)
        back_label = await i18n.t("common.back_to_main", lang)
        markup = kb.trial_success_kb(subs_label=subs_label, back_label=back_label)
    elif etype == "access_offered":
        text = await i18n.t(
            "notification.access_offered",
            lang,
            tariff_name=h(payload.get("tariff_name", "")),
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
            email=h(payload.get("email", "")),
            expires=await _format_expires_at(payload.get("expiry_time_ms"), i18n=i18n, lang=lang),
        )

        rows = []
        tariff_id = payload.get("tariff_id")
        if renewable and tariff_id:
            renew_label = await i18n.t("notification.button.renew", lang)
            rows.append([_types.InlineKeyboardButton(text=renew_label, callback_data=f"buy:{tariff_id}")])
        home_label = await i18n.t("common.back_to_main", lang)
        rows.append([_types.InlineKeyboardButton(text=home_label, callback_data="user_home")])
        keyboard = _types.InlineKeyboardMarkup(inline_keyboard=rows)
        await bot.send_message(tg_id, text, reply_markup=keyboard)
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
            email=h(payload.get("email", "")),
            used=format_bytes(used),
            limit=format_bytes(limit),
            remaining=format_bytes(remaining),
        )
        rows = []
        tariff_id = payload.get("tariff_id")
        if renewable and tariff_id:
            renew_label = await i18n.t("notification.button.renew", lang)
            rows.append([_types.InlineKeyboardButton(text=renew_label, callback_data=f"buy:{tariff_id}")])
        home_label = await i18n.t("common.back_to_main", lang)
        rows.append([_types.InlineKeyboardButton(text=home_label, callback_data="user_home")])
        keyboard = _types.InlineKeyboardMarkup(inline_keyboard=rows)
        await bot.send_message(tg_id, text, reply_markup=keyboard)
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
    await bot.send_message(tg_id, text, reply_markup=markup, parse_mode="HTML")


async def _receive_events(enqueue) -> None:
    uri = _redis_uri()
    if uri is None:
        logger.warning("bot_events_consumer: no redis URI; events disabled")
        return

    backoff = 1.0
    while True:
        client = None
        pubsub = None
        failed = False
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
                    if not isinstance(event, dict) or not isinstance(event.get("payload", {}), dict):
                        raise ValueError("invalid event shape")
                except (ValueError, TypeError, KeyError):
                    logger.warning("bot_events_consumer: discarded malformed event")
                    continue
                enqueue(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed = True
            logger.warning("bot_events_consumer: %s — reconnecting in %.1fs", type(exc).__name__, backoff)
        finally:
            for resource in (pubsub, client):
                if resource is not None:
                    try:
                        await resource.aclose()
                    except Exception as exc:
                        logger.warning("bot_events_consumer: resource close failed: %s", type(exc).__name__)
        if failed:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def run_consumer(bot_source: BotSource, i18n: I18n, middleware=None, backend=None) -> None:
    if backend is None:
        logger.error("bot event delivery requires a backend client")
        return
    queue = asyncio.Queue(maxsize=256)
    active = set()

    def enqueue(event):
        key = (event.get("source"), event.get("id"))
        if not isinstance(key[0], str) or not isinstance(key[1], int):
            logger.warning("bot_events_consumer: event has no stable identity")
            return
        if key in active:
            return
        try:
            queue.put_nowait((key, event))
            active.add(key)
        except asyncio.QueueFull:
            logger.warning("bot event queue full; durable outbox or inbox will retry")

    async def work():
        while True:
            key, event = await queue.get()
            try:
                await _handle(event, bot_source, i18n, middleware, backend)
            except Exception as exc:
                logger.error("bot event processing failed: %s", type(exc).__name__)
            finally:
                active.discard(key)
                queue.task_done()

    async def retry_pending():
        while True:
            try:
                for event in await backend.pending_events():
                    enqueue(event)
            except Exception as exc:
                logger.warning("pending bot deliveries unavailable: %s", type(exc).__name__)
            await asyncio.sleep(5)

    tasks = [asyncio.create_task(work()) for _ in range(4)]
    tasks.extend((asyncio.create_task(_receive_events(enqueue)), asyncio.create_task(retry_pending())))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
