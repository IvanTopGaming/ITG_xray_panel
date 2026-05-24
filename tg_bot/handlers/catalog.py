"""Flow 3 — tariff catalog & purchase."""

import logging

import httpx
from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

from backend_client import BackendClient
from i18n import I18n

logger = logging.getLogger(__name__)
router = Router()


async def _tariff_card(tariff: dict, *, i18n: I18n, lang: str) -> str:
    header = await i18n.t(
        "catalog.tariff_card.header",
        lang,
        name=tariff["name"],
        price=tariff["price_rub"],
        days=tariff["period_days"],
    )
    lines = [header]
    for item in tariff["items"]:
        display = item.get("label") or item.get("inbound_tag", "")
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
    except Exception as exc:
        logger.warning("catalog: list_tariffs failed: %s", exc)
        await callback.answer("Service temporarily unavailable.", show_alert=True)
        return
    if not tariffs:
        back = await i18n.t("common.back_to_main", lang)
        await callback.message.edit_text(
            await i18n.t("catalog.empty", lang),
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


@router.callback_query(F.data.startswith("buy:"))
async def start_checkout(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):
    try:
        tariff_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Bad request.", show_alert=True)
        return
    try:
        result = await backend.create_checkout(callback.from_user.id, tariff_id, lang)
    except httpx.HTTPStatusError as exc:
        err_code = None
        if exc.response.status_code == 400:
            try:
                err_code = exc.response.json().get("error")
            except ValueError:
                err_code = None
        if err_code == "tariff_not_available":
            msg = await i18n.t("catalog.tariff_not_available", lang)
            await callback.answer(msg, show_alert=True)
            return
        logger.warning("catalog: create_checkout failed: %s", exc)
        fallback = await i18n.t("errors.checkout_unavailable", lang)
        await callback.answer(fallback, show_alert=True)
        return
    except Exception as exc:
        logger.warning("catalog: create_checkout failed: %s", exc)
        fallback = await i18n.t("errors.checkout_unavailable", lang)
        await callback.answer(fallback, show_alert=True)
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
    msg = await callback.message.edit_text(text, reply_markup=keyboard)
    try:
        await backend.set_payment_chat_coords(
            result["payment_id"],
            chat_id=msg.chat.id,
            message_id=msg.message_id,
        )
    except Exception as exc:
        logger.warning(
            "set_payment_chat_coords failed for payment=%s: %s",
            result["payment_id"],
            exc,
        )
    await callback.answer()


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
        result = await backend.cancel_payment(payment_id)
        status = (result or {}).get("status")
    except Exception as exc:
        logger.warning("catalog: cancel_payment failed: %s", exc)

    # Race with the YooKassa webhook: if the payment already succeeded, the
    # push notification has either already replaced this message or is about
    # to. Don't lie to the user with a "cancelled" screen.
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
        # Push notification already deleted the checkout message — nothing to edit.
        logger.info("cancel_payment: edit_text skipped (message gone): %s", exc)
