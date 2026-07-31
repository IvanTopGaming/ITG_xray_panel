from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
from html import escape
from typing import Any, Awaitable, Callable, Optional, Union

import redis.asyncio as redis_async
from aiogram import Bot
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


_NODE_EVENT_TYPES = ("expiry_notification", "traffic_notification")


_SCOPE_MAX = 200


def _bounded_scope(scope: str) -> str:
    if len(scope) <= _SCOPE_MAX:
        return scope
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return scope[: _SCOPE_MAX - len(digest) - 1] + "|" + digest


def _claim_scope(etype: str, payload: dict) -> str:
    client_scope = "{}/{}/{}".format(
        payload.get("node", ""),
        payload.get("inbound_tag", ""),
        payload.get("email", ""),
    )
    if etype == "traffic_notification":
        return _bounded_scope("{}/{}".format(client_scope, payload.get("cycle") or 0))
    if payload.get("tariff_id"):
        return ""
    return _bounded_scope(client_scope)


async def _resolve_claim(etype, tg_id, payload, backend) -> tuple[bool, str, bool]:
    if backend is None:
        return True, payload.get("lang", "ru"), bool(payload.get("renewable"))
    try:
        verdict = await backend.claim_notification(
            telegram_id=int(tg_id),
            kind=payload.get("kind", ""),
            tariff_id=payload.get("tariff_id"),
            scope=_claim_scope(etype, payload),
        )
    except Exception as exc:
        logger.warning("claim failed for %s/%s: %s - sending anyway", tg_id, etype, exc)
        return True, "ru", False
    return bool(verdict.get("claimed")), verdict.get("lang", "ru"), bool(verdict.get("renewable"))


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
    bot = await _resolve_bot(bot_source)
    etype = event.get("type")
    tg_id = event.get("telegram_id")
    payload = event.get("payload") or {}
    lang = payload.get("lang", "ru")

    if etype in _NODE_EVENT_TYPES:
        claimed, lang, renewable = await _resolve_claim(etype, tg_id, payload, backend)
        if not claimed:
            return
    else:
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
    elif etype == "access_renewed":
        text = await i18n.t("notification.access_renewed", lang)
    elif etype == "access_paused":
        text = await i18n.t(
            "notification.access_paused",
            lang,
            tariff_name=h(payload.get("tariff_name", "")),
        )
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
    elif etype == "access_granted_once":
        text = await i18n.t(
            "notification.access_granted_once",
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
            email=payload.get("email", ""),
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
        if renewable and tariff_id:
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


async def run_consumer(bot_source: BotSource, i18n: I18n, middleware=None, backend=None) -> None:
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
                await _handle(event, bot_source, i18n, middleware, backend)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("bot_events_consumer: %s — reconnecting in %.1fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
