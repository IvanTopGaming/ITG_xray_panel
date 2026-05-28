from typing import Optional

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


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
        full = name
        buttons.append(
            [InlineKeyboardButton(text=entry_template.format(name=full), callback_data=f"show_key_{client_id}")]
        )
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
            [InlineKeyboardButton(text=qr_label, callback_data="qr_select_server")],
            [InlineKeyboardButton(text=stats_label, callback_data="user_stats")],
            [InlineKeyboardButton(text=back_label, callback_data=back_callback)],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def user_qr_server_kb(
    panels,
    *,
    server_template: str,
    back_label: str,
) -> InlineKeyboardMarkup:
    buttons = []
    for idx, p in enumerate(panels):
        buttons.append([InlineKeyboardButton(text=server_template.format(name=p.name), callback_data=f"qr_gen_{idx}")])
    buttons.append([InlineKeyboardButton(text=back_label, callback_data="back_to_keys")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def qr_back_kb(*, back_label: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=back_label, callback_data="back_to_keys")]]
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
        [InlineKeyboardButton(text=show_again_label, callback_data=f"show_key_{client_id}")],
        [InlineKeyboardButton(text=back_label, callback_data="user_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_main_kb():
    buttons = [
        [
            InlineKeyboardButton(text="🖥 System Resources", callback_data="admin_system"),
            InlineKeyboardButton(text="🔄 Restart Core", callback_data="admin_restart_menu"),
        ],
        [InlineKeyboardButton(text="📦 Backup & Restore", callback_data="admin_backups_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_backups_kb():
    buttons = [
        [InlineKeyboardButton(text="⬇️ Download PANEL DB", callback_data="backup_dl_panel_menu")],
        [InlineKeyboardButton(text="⬆️ Restore Data", callback_data="admin_backup_restore")],
        [InlineKeyboardButton(text="⬅️ Back to Dashboard", callback_data="admin_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def server_selection_kb(panels, action_prefix, include_all=False, linked_panels=None):
    buttons = []
    if include_all:
        buttons.append([InlineKeyboardButton(text="🌐 All Servers", callback_data=f"{action_prefix}all")])

    for idx, panel in enumerate(panels):
        buttons.append([InlineKeyboardButton(text=f"💻 {panel.name}", callback_data=f"{action_prefix}{idx}")])

    for lp in linked_panels or []:
        if lp.get("enable", True):
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🔗 {lp.get('name', 'Panel')}",
                        callback_data=f"{action_prefix}lp_{lp['id']}",
                    )
                ]
            )

    buttons.append([InlineKeyboardButton(text="⬅️ Cancel", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_restore_type_kb():
    buttons = [
        [InlineKeyboardButton(text="🎛 Restore PANEL DB", callback_data="restore_type_panel")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_cancel_restore")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_back_kb():
    buttons = [[InlineKeyboardButton(text="⬅️ Back to Dashboard", callback_data="admin_home")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_restart_kb(target):
    buttons = [
        [
            InlineKeyboardButton(text="✅ Yes, Restart", callback_data=f"confirm_restart_{target}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="admin_home"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def language_picker_kb(en_label: str, ru_label: str) -> InlineKeyboardMarkup:
    """Two-button row: English / Русский. Callback data set_lang:en|ru."""
    buttons = [
        [
            InlineKeyboardButton(text=en_label, callback_data="set_lang:en"),
            InlineKeyboardButton(text=ru_label, callback_data="set_lang:ru"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def first_touch_kb(*, activate_label: str, skip_label: str) -> InlineKeyboardMarkup:
    """Two-button row for the onboarding screen: activate trial / skip."""
    buttons = [
        [
            InlineKeyboardButton(text=activate_label, callback_data="trial:activate"),
            InlineKeyboardButton(text=skip_label, callback_data="trial:skip"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def trial_success_kb(*, subs_label: str, back_label: str) -> InlineKeyboardMarkup:
    """Two stacked buttons shown after trial activation: jump to subscription
    keys, or fall back to the main menu."""
    buttons = [
        [InlineKeyboardButton(text=subs_label, callback_data="user_sub")],
        [InlineKeyboardButton(text=back_label, callback_data="user_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def payment_retry_kb(*, tariffs_label: str, back_label: str) -> InlineKeyboardMarkup:
    """Two-button column shown after payment_cancelled or payment_failed:
    open tariffs to try again, or go back to main menu."""
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
    """Menu for users without active clients. The trial row appears only
    when `trial_label` is truthy."""
    rows = []
    if trial_label:
        rows.append([InlineKeyboardButton(text=trial_label, callback_data="trial:activate")])
    rows.append([InlineKeyboardButton(text=tariffs_label, callback_data="tariffs:list")])
    rows.append([InlineKeyboardButton(text=help_label, callback_data="user_help")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
