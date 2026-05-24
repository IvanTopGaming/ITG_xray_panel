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


def server_selection_kb(panels, action_prefix, include_all=False):
    buttons = []
    if include_all:
        buttons.append([InlineKeyboardButton(text="🌐 All Servers", callback_data=f"{action_prefix}all")])

    for idx, panel in enumerate(panels):
        buttons.append([InlineKeyboardButton(text=f"💻 {panel.name}", callback_data=f"{action_prefix}{idx}")])

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


def inbounds_kb(inbounds, mode="create"):
    buttons = []
    prefix = "inbound_select_" if mode == "create" else "link_inbound_"
    for i in inbounds:
        tag = i["tag"]
        proto = i["protocol"]
        buttons.append([InlineKeyboardButton(text=f"🌐 {tag} ({proto})", callback_data=f"{prefix}{tag}")])
    buttons.append([InlineKeyboardButton(text="❌ Cancel Operation", callback_data="admin_cancel_add")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def skip_kb():
    buttons = [[InlineKeyboardButton(text="⏭ Skip / Unlimited", callback_data="skip_step")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def edit_limit_kb(db_id):
    buttons = [
        [InlineKeyboardButton(text="♾️ Make Unlimited", callback_data="skip_step")],
        [InlineKeyboardButton(text="⬅️ Cancel", callback_data=f"manage_user_{db_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def user_list_kb(users, page=0):
    items_per_page = 5
    start = page * items_per_page
    end = start + items_per_page
    current_page_users = users[start:end]

    buttons = []
    for u in current_page_users:
        db_id = u[0]
        tg_id = u[1]
        tg_username = f"@{u[2]}" if u[2] else f"ID: {tg_id}"
        key_name = u[3]
        inbound_tag = str(u[4] or "").strip()
        inbound_suffix = f" [{inbound_tag}]" if inbound_tag and inbound_tag.lower() != "multi" else ""

        status_icon = "👤"
        label = f"{status_icon} {key_name}{inbound_suffix} ({tg_username})"

        buttons.append([InlineKeyboardButton(text=label, callback_data=f"manage_user_{db_id}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"users_page_{page - 1}"))
    if end < len(users):
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"users_page_{page + 1}"))

    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([InlineKeyboardButton(text="⬅️ Back to Dashboard", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def add_user_type_kb():
    buttons = [
        [InlineKeyboardButton(text="🆕 New Telegram ID", callback_data="add_user_new")],
        [InlineKeyboardButton(text="🔗 Existing User", callback_data="add_user_existing")],
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Cancel", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def existing_users_selection_kb(unique_users, page=0):
    items_per_page = 5
    start = page * items_per_page
    end = start + items_per_page
    current = unique_users[start:end]

    buttons = []
    for u in current:
        tg_id = u[0]
        username = u[1]
        label = f"@{username}" if username else f"ID: {tg_id}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"sel_exist_{tg_id}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"exist_page_{page - 1}"))
    if end < len(unique_users):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"exist_page_{page + 1}"))

    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="admin_cancel_add")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def client_list_kb(clients, page=0):
    items_per_page = 5
    start = page * items_per_page
    end = start + items_per_page
    current_page_clients = clients[start:end]

    buttons = []
    for idx, c in enumerate(current_page_clients, start=start):
        label = f"📧 {c['email']}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"link_client_{idx}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"link_page_{page - 1}"))
    if end < len(clients):
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"link_page_{page + 1}"))

    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="admin_cancel_add")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def user_manage_actions_kb(db_id, is_enabled, sync_errors=None):
    enable_text = "🔴 Disable Key" if is_enabled else "🟢 Enable Key"
    enable_callback = f"toggle_enable_{db_id}"

    buttons = [
        [InlineKeyboardButton(text=enable_text, callback_data=enable_callback)],
        [
            InlineKeyboardButton(text="📊 Edit Data Limit", callback_data=f"edit_limit_{db_id}"),
            InlineKeyboardButton(text="📅 Edit Expiry", callback_data=f"edit_expiry_{db_id}"),
        ],
        [
            InlineKeyboardButton(text="✏️ Edit Key Name", callback_data=f"edit_keyname_{db_id}"),
        ],
        [
            InlineKeyboardButton(text="👤 Edit Owner Username", callback_data=f"edit_username_{db_id}"),
            InlineKeyboardButton(text="🆔 Transfer Key (Change ID)", callback_data=f"edit_tgid_{db_id}"),
        ],
    ]

    if sync_errors:
        for err_server, _ in sync_errors:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🔁 Retry: {err_server}",
                        callback_data=f"retry_sync_{db_id}_{err_server}",
                    )
                ]
            )

    buttons.extend(
        [
            [InlineKeyboardButton(text="🔄 Reset Traffic Used", callback_data=f"ask_reset_{db_id}")],
            [InlineKeyboardButton(text="🗑 Delete Key", callback_data=f"ask_delete_{db_id}")],
            [InlineKeyboardButton(text="⬅️ Back to List", callback_data="admin_users")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def expiry_edit_options_kb(db_id):
    buttons = [
        [InlineKeyboardButton(text="➕ Extend 30 Days", callback_data=f"extend_30_{db_id}")],
        [InlineKeyboardButton(text="♾️ Make Permanent", callback_data=f"make_permanent_{db_id}")],
        [InlineKeyboardButton(text="⌨️ Custom Days", callback_data=f"custom_days_{db_id}")],
        [InlineKeyboardButton(text="⬅️ Cancel", callback_data=f"manage_user_{db_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_restart_kb(target):
    buttons = [
        [
            InlineKeyboardButton(text="✅ Yes, Restart", callback_data=f"confirm_restart_{target}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="admin_home"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_delete_kb(db_id):
    buttons = [
        [
            InlineKeyboardButton(text="🗑 Yes, Delete Key", callback_data=f"confirm_delete_{db_id}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data=f"manage_user_{db_id}"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_reset_traffic_kb(db_id):
    buttons = [
        [
            InlineKeyboardButton(text="✅ Yes, Reset", callback_data=f"confirm_reset_{db_id}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data=f"manage_user_{db_id}"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def edit_username_kb(db_id):
    buttons = [
        [InlineKeyboardButton(text="🗑 Clear Username", callback_data=f"clear_username_{db_id}")],
        [InlineKeyboardButton(text="⬅️ Cancel", callback_data=f"cancel_edit_{db_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def edit_cancel_kb(db_id):
    buttons = [
        [InlineKeyboardButton(text="⬅️ Cancel", callback_data=f"cancel_edit_{db_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def skip_username_kb():
    buttons = [
        [InlineKeyboardButton(text="⏭ Skip", callback_data="skip_username")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_cancel_add")],
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
