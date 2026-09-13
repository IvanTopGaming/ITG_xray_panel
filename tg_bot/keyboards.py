from typing import Optional
import hashlib

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from i18n import format_text


def key_callback(client_id) -> str:
    value = f"show_key_{client_id}"
    if len(value.encode("utf-8")) <= 64:
        return value
    return "show_key_hash_" + hashlib.sha256(str(client_id).encode("utf-8")).hexdigest()[:40]


def user_main_kb(
    *,
    subs_label: str,
    tariffs_label: str,
    stats_label: str,
    help_label: str,
) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=subs_label, callback_data="user_sub")],
        [InlineKeyboardButton(text=tariffs_label, callback_data="tariffs:list")],
        [InlineKeyboardButton(text=stats_label, callback_data="user_stats")],
        [InlineKeyboardButton(text=help_label, callback_data="user_help")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def user_keys_list_kb(
    records,
    *,
    entry_template: str,
    back_label: str,
) -> InlineKeyboardMarkup:
    buttons = []
    for r in records:
        client_id = r["id"]
        name = r.get("inbound_label") or r.get("inbound_tag") or client_id
        try:
            label = format_text(entry_template, {"name": name})
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            label = f"🔑 {name}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=key_callback(client_id))])
    buttons.append([InlineKeyboardButton(text=back_label, callback_data="user_home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def key_picker_kb(
    links: list[str],
    *,
    back_label: str,
    back_callback: str = "user_home",
) -> InlineKeyboardMarkup:
    buttons = []
    for i, link in enumerate(links):
        label = link.rsplit("#", 1)[-1] if "#" in link else f"Key {i + 1}"
        from urllib.parse import unquote

        label = unquote(label)
        buttons.append([InlineKeyboardButton(text=f"🔑 {label}", callback_data=f"show_link_{i}")])
    buttons.append([InlineKeyboardButton(text=back_label, callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def sub_actions_kb(
    *,
    qr_label: str,
    stats_label: str,
    back_label: str,
    back_callback: str = "user_home",
    renew_label: str | None = None,
    renew_tariff_id: int | None = None,
) -> InlineKeyboardMarkup:
    buttons = []
    if renew_label and renew_tariff_id:
        buttons.append([InlineKeyboardButton(text=renew_label, callback_data=f"buy:{renew_tariff_id}")])
    buttons.extend(
        [
            [InlineKeyboardButton(text=qr_label, callback_data="show_qr")],
            [InlineKeyboardButton(text=stats_label, callback_data="user_stats")],
            [InlineKeyboardButton(text=back_label, callback_data=back_callback)],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def qr_back_kb(*, back_label: str, back_callback: str = "back_to_keys") -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=back_label, callback_data=back_callback)]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def user_sub_page_kb(
    *,
    open_label: str,
    keys_label: str,
    help_label: str,
    back_label: str,
    sub_url: str | None = None,
    qr_label: str | None = None,
    renew_label: str | None = None,
    renew_tariff_id: int | None = None,
) -> InlineKeyboardMarkup:

    buttons = []
    if sub_url:
        buttons.append([InlineKeyboardButton(text=open_label, url=sub_url)])
        if qr_label:
            buttons.append([InlineKeyboardButton(text=qr_label, callback_data="sub_qr")])
    if renew_label and renew_tariff_id:
        buttons.append([InlineKeyboardButton(text=renew_label, callback_data=f"buy:{renew_tariff_id}")])
    buttons.append([InlineKeyboardButton(text=keys_label, callback_data="show_keys")])
    buttons.append([InlineKeyboardButton(text=help_label, callback_data="user_help")])
    buttons.append([InlineKeyboardButton(text=back_label, callback_data="user_home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_main_kb(*, back_label: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=back_label, callback_data="user_home")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def user_stats_kb(*, refresh_label: str, back_label: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=refresh_label, callback_data="user_stats")],
        [InlineKeyboardButton(text=back_label, callback_data="user_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def expired_keys_kb(*, show_again_label: str, back_label: str, client_id: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=show_again_label, callback_data=key_callback(client_id))],
        [InlineKeyboardButton(text=back_label, callback_data="user_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def language_picker_kb(en_label: str, ru_label: str) -> InlineKeyboardMarkup:

    buttons = [
        [
            InlineKeyboardButton(text=en_label, callback_data="set_lang:en"),
            InlineKeyboardButton(text=ru_label, callback_data="set_lang:ru"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def first_touch_kb(*, activate_label: str, skip_label: str) -> InlineKeyboardMarkup:

    buttons = [
        [
            InlineKeyboardButton(text=activate_label, callback_data="trial:activate"),
            InlineKeyboardButton(text=skip_label, callback_data="trial:skip"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def trial_success_kb(*, subs_label: str, back_label: str) -> InlineKeyboardMarkup:

    buttons = [
        [InlineKeyboardButton(text=subs_label, callback_data="user_sub")],
        [InlineKeyboardButton(text=back_label, callback_data="user_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def payment_retry_kb(*, tariffs_label: str, back_label: str) -> InlineKeyboardMarkup:

    buttons = [
        [InlineKeyboardButton(text=tariffs_label, callback_data="tariffs:list")],
        [InlineKeyboardButton(text=back_label, callback_data="user_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def no_clients_menu_kb(
    *,
    trial_label: Optional[str],
    tariffs_label: str,
    help_label: str,
) -> InlineKeyboardMarkup:

    rows = []
    if trial_label:
        rows.append([InlineKeyboardButton(text=trial_label, callback_data="trial:activate")])
    rows.append([InlineKeyboardButton(text=tariffs_label, callback_data="tariffs:list")])
    rows.append([InlineKeyboardButton(text=help_label, callback_data="user_help")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
