import asyncio
import logging
from html import escape

import httpx
from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

from backend_client import BackendClient
from i18n import I18n

logger = logging.getLogger(__name__)
router = Router()

_checkout_tasks: set[asyncio.Task] = set()
_checkout_in_flight: set[int] = set()


def h(value):
    return escape(str(value), quote=True)


async def _tariff_card(tariff: dict, *, i18n: I18n, lang: str) -> str:
    header = await i18n.t(
        "catalog.tariff_card.header",
        lang,
        name=h(tariff["name"]),
        price=tariff["price_rub"],
        days=tariff["period_days"],
    )
    lines = [header]
    for item in tariff["items"]:
        display = h(item.get("label") or item.get("inbound_label") or item.get("inbound_tag", ""))
        if item["traffic_gb"]:
            amount = await i18n.t(
                "catalog.tariff_card.item.gb_amount",
                lang,
                gb=item["traffic_gb"],
            )
        else:
            amount = await i18n.t("catalog.tariff_card.item.unlimited_amount", lang)
        line = await i18n.t(
            "catalog.tariff_card.item",
            lang,
            display=display,
            amount=amount,
        )
        lines.append(line)
    return "\n".join(lines)


async def _catalog_keyboard(
    tariffs: list[dict],
    *,
    i18n: I18n,
    lang: str,
) -> types.InlineKeyboardMarkup:
    back = await i18n.t("common.back_to_main", lang)
    marker = await i18n.t("catalog.button.active_marker", lang)
    rows = []
    for t in tariffs:
        display_name = t["name"]
        if t.get("is_active"):
            display_name = f"{display_name} {marker}"
        label = await i18n.t(
            "catalog.button.buy_inline",
            lang,
            name=display_name,
            price=t["price_rub"],
        )
        rows.append([types.InlineKeyboardButton(text=label, callback_data=f"buy:{t['id']}")])
    rows.append([types.InlineKeyboardButton(text=back, callback_data="user_home")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "tariffs:list")
async def show_catalog(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):
    try:
        tariffs = await backend.list_tariffs(callback.from_user.id)
    except Exception:
        logger.exception("catalog: list_tariffs failed")
        await callback.answer("Service temporarily unavailable.", show_alert=True)
        return
    if not tariffs:
        open_ended = False
        try:
            open_ended = bool((await backend.get_user_state(callback.from_user.id)).get("open_ended_access"))
        except Exception:
            logger.exception("catalog: user state lookup failed")
        back = await i18n.t("common.back_to_main", lang)
        await callback.message.edit_text(
            await i18n.t("catalog.open_ended" if open_ended else "catalog.empty", lang),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=back, callback_data="user_home")],
                ]
            ),
        )
        return
    cards = [await _tariff_card(t, i18n=i18n, lang=lang) for t in tariffs]
    text = "\n\n".join(cards)
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=await _catalog_keyboard(tariffs, i18n=i18n, lang=lang),
    )
    await callback.answer()


async def _show_checkout_error(message, key, *, i18n: I18n, lang: str):
    text = await i18n.t(key, lang)
    back = await i18n.t("common.back_to_main", lang)
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=back, callback_data="user_home")],
        ]
    )
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        logger.info("start_checkout: could not show the checkout error (message gone): %s", exc)


async def _finish_checkout(
    *,
    message,
    telegram_id: int,
    tariff_id: int,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):

    try:
        try:
            result = await backend.create_checkout(telegram_id, tariff_id, lang)
        except httpx.HTTPStatusError as exc:
            err_code = None
            if exc.response.status_code == 400:
                try:
                    err_code = exc.response.json().get("error")
                except ValueError:
                    err_code = None
            if err_code == "tariff_not_available":
                await _show_checkout_error(message, "catalog.tariff_not_available", i18n=i18n, lang=lang)
                return
            logger.exception("catalog: create_checkout failed")
            await _show_checkout_error(message, "errors.checkout_unavailable", i18n=i18n, lang=lang)
            return
        except Exception:
            logger.exception("catalog: create_checkout failed")
            await _show_checkout_error(message, "errors.checkout_unavailable", i18n=i18n, lang=lang)
            return

        text = await i18n.t(
            "catalog.pay_prompt",
            lang,
            amount=str(result["amount_rub"]),
        )
        pay_label = await i18n.t("checkout.button.pay", lang, price=result["amount_rub"])
        cancel_label = await i18n.t("common.cancel", lang)
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=pay_label, url=result["confirmation_url"])],
                [types.InlineKeyboardButton(text=cancel_label, callback_data=f"cancel:{result['payment_id']}")],
            ]
        )
        try:
            msg = await message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            logger.info("start_checkout: could not show the pay button (message gone): %s", exc)
            return
        try:
            await backend.set_payment_chat_coords(
                result["payment_id"],
                chat_id=msg.chat.id,
                message_id=msg.message_id,
                telegram_id=telegram_id,
            )
        except Exception as exc:
            logger.info("set_payment_chat_coords failed for payment=%s: %s", result["payment_id"], exc)
    finally:
        _checkout_in_flight.discard(telegram_id)


@router.callback_query(F.data.startswith("buy:"))
async def start_checkout(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):
    """Answer Telegram first, build the invoice afterwards.

    `create_checkout` calls YooKassa with an 8-second timeout and one retry, so up to ~16 seconds pass
    before it returns. Doing that inside the callback left the button spinning with nothing said. The
    answer now goes out immediately and the catalogue is replaced by a line saying the invoice is
    being created, so the same message becomes either the pay screen or an error once YooKassa has
    replied and the wait is never unexplained.
    """

    try:
        tariff_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Bad request.", show_alert=True)
        return

    telegram_id = callback.from_user.id
    if telegram_id in _checkout_in_flight:
        await callback.answer()
        return
    _checkout_in_flight.add(telegram_id)

    await callback.answer()

    message = callback.message
    try:
        await message.edit_text(await i18n.t("checkout.creating", lang), reply_markup=None)
    except TelegramBadRequest as exc:
        logger.info("start_checkout: could not show the invoice placeholder: %s", exc)

    task = asyncio.create_task(
        _finish_checkout(
            message=message,
            telegram_id=telegram_id,
            tariff_id=tariff_id,
            i18n=i18n,
            lang=lang,
            backend=backend,
        )
    )
    _checkout_tasks.add(task)
    task.add_done_callback(_checkout_tasks.discard)


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_payment(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):
    try:
        payment_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Bad request.", show_alert=True)
        return
    status = None
    try:
        result = await backend.cancel_payment(payment_id, telegram_id=callback.from_user.id)
        status = (result or {}).get("status")
    except Exception as exc:
        logger.info("catalog: cancel_payment failed: %s", exc)

    if status == "succeeded":
        await callback.answer()
        return

    await callback.answer()
    back = await i18n.t("common.back_to_main", lang)
    message = await i18n.t("catalog.payment_cancelled.message", lang)
    home_kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=back, callback_data="user_home")],
        ]
    )
    try:
        await callback.message.edit_text(message, reply_markup=home_kb)
    except TelegramBadRequest as exc:
        logger.info("cancel_payment: edit_text skipped (message gone): %s", exc)
