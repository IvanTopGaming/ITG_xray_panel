from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def user_main_kb():
    buttons = [
        [InlineKeyboardButton(text="🚀 My Subscription", callback_data="user_sub")],
        [InlineKeyboardButton(text="📊 Usage Stats", callback_data="user_stats")],
        [InlineKeyboardButton(text="ℹ️ Help / Setup Guide", callback_data="user_help")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def user_keys_list_kb(records):
    buttons = []
    for r in records:
        db_id = r[0]
        name = r[3]
        inbound_tag = str(r[4] or "").strip()
        suffix = f" [{inbound_tag}]" if inbound_tag and inbound_tag.lower() != "multi" else ""
        buttons.append([InlineKeyboardButton(text=f"🔑 {name}{suffix}", callback_data=f"show_key_{db_id}")])

    buttons.append([InlineKeyboardButton(text="⬅️ Main Menu", callback_data="user_home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def sub_actions_kb():
    buttons = [
        [InlineKeyboardButton(text="📱 Show QR Code", callback_data="qr_select_server")],
        [InlineKeyboardButton(text="📊 My Stats", callback_data="user_stats")],
        [InlineKeyboardButton(text="⬅️ Main Menu", callback_data="user_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def user_qr_server_kb(panels):
    buttons = []
    for idx, p in enumerate(panels):
        buttons.append([InlineKeyboardButton(text=f"💻 {p.name}", callback_data=f"qr_gen_{idx}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Back to Keys", callback_data="back_to_keys")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def qr_back_kb():
    buttons = [[InlineKeyboardButton(text="⬅️ Back to Keys", callback_data="back_to_keys")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_main_kb():
    buttons = [[InlineKeyboardButton(text="⬅️ Back to Main Menu", callback_data="user_home")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_main_kb():
    buttons = [
        [InlineKeyboardButton(text="👥 User Management", callback_data="admin_users")],
        [
            InlineKeyboardButton(text="🖥 System Resources", callback_data="admin_system"),
            InlineKeyboardButton(text="🔄 Restart Core", callback_data="admin_restart_menu"),
        ],
        [InlineKeyboardButton(text="♻️ Force Sync Users", callback_data="admin_force_sync")],
        [
            InlineKeyboardButton(text="➕ Generate New User", callback_data="admin_add_user"),
            InlineKeyboardButton(text="🔗 Link Existing", callback_data="admin_link_user"),
        ],
        [InlineKeyboardButton(text="📦 Backup & Restore", callback_data="admin_backups_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_backups_kb():
    buttons = [
        [
            InlineKeyboardButton(text="⬇️ Download BOT DB", callback_data="backup_dl_bot"),
            InlineKeyboardButton(text="⬇️ Download PANEL DB", callback_data="backup_dl_panel_menu"),
        ],
        [InlineKeyboardButton(text="🚀 Full Backup Now", callback_data="admin_force_backup")],
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
        [
            InlineKeyboardButton(text="🤖 Restore BOT DB", callback_data="restore_type_bot"),
            InlineKeyboardButton(text="🎛 Restore PANEL DB", callback_data="restore_type_panel"),
        ],
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
