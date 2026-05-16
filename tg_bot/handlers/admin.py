import asyncio
import os
import sys
import uuid
import logging
import re
import sqlite3
import tempfile
from html import escape
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
from config import ADMIN_IDS
from database import db
from api_service import panel_api
from states import (
    AddUserStates,
    EditUserStates,
    LinkUserStates,
    RestoreStates,
    BackupStates,
)
import keyboards as kb
from utils import bot_backup_filename, format_bytes, panel_backup_filename
from jobs import sync_users_across_panels, send_backup
import time
import datetime

router = Router()
logger = logging.getLogger(__name__)
ADMIN_ID_SET = set()

for admin_id in ADMIN_IDS:
    try:
        ADMIN_ID_SET.add(int(admin_id))
    except (TypeError, ValueError):
        continue

router.message.filter(F.from_user.id.in_(list(ADMIN_ID_SET)))
router.callback_query.filter(F.from_user.id.in_(list(ADMIN_ID_SET)))


def h(value):
    return escape(str(value), quote=True)


def _parse_callback_int(callback_data, index=2):
    try:
        return int(str(callback_data).split("_")[index])
    except (TypeError, ValueError, IndexError):
        return None


def _message_text(message: types.Message):
    return (message.text or "").strip()


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
EMAIL_RE = re.compile(r"^[^\s\x00-\x1F\x7F]{1,100}$")
MAX_RESTORE_FILE_BYTES = 50 * 1024 * 1024
SQLITE_HEADER = b"SQLite format 3\x00"


def is_admin(user_id):
    return user_id in ADMIN_ID_SET


def _validate_sqlite_bytes(content: bytes):
    if not isinstance(content, (bytes, bytearray)) or not content:
        return "Invalid backup file"
    if len(content) > MAX_RESTORE_FILE_BYTES:
        return "File is too large (max 50 MB)."
    if not bytes(content).startswith(SQLITE_HEADER):
        return "Unsupported backup format"

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="bot-restore-",
            suffix=".db",
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name

        with sqlite3.connect(temp_path) as conn:
            row = conn.execute("PRAGMA integrity_check;").fetchone()
            status = str(row[0]).strip().lower() if row else ""
            if status != "ok":
                return "Backup integrity check failed"
    except sqlite3.DatabaseError:
        return "Backup integrity check failed"
    except OSError:
        return "Failed to validate backup file"
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    return None


async def safe_get_stats(user_email, inbound_tag=None):
    try:
        return await panel_api.get_client_stats_aggregate(user_email, inbound_tag=inbound_tag)
    except Exception:
        return None


@router.message(Command("admin"))
async def admin_start(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    alive_count = 0
    for p in panel_api.panels:
        try:
            if await p.login():
                alive_count += 1
        except Exception as exc:
            logger.debug("Panel login failed for %s: %s", p.name, exc)

    status_text = f"✅ {alive_count}/{len(panel_api.panels)} Online"

    await message.answer(
        f"🛠 <b>Administrator Dashboard</b>\nNodes Status: {status_text}",
        reply_markup=kb.admin_main_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_home")
async def admin_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🛠 <b>Administrator Dashboard</b>\n\nSelect an action:",
        reply_markup=kb.admin_main_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_backups_menu")
async def admin_backups_menu(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📦 <b>Backup & Restore Manager</b>\n\n"
        "Select an action:\n"
        "⬇️ <b>Download</b>: Get current Bot and Panel DBs.\n"
        "⬆️ <b>Restore</b>: Upload a DB file to restore.",
        reply_markup=kb.admin_backups_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "backup_dl_bot")
async def admin_backup_dl_bot(callback: types.CallbackQuery):
    db_path = "db/bot.db"
    if os.path.exists(db_path):
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        await callback.answer("Sending file...")
        await callback.message.answer_document(
            FSInputFile(db_path, filename=bot_backup_filename(date_str)),
            caption="🤖 Bot Database",
        )
    else:
        await callback.answer("Bot DB not found!", show_alert=True)


@router.callback_query(F.data == "backup_dl_panel_menu")
async def admin_backup_dl_panel_menu(callback: types.CallbackQuery, state: FSMContext):
    if not panel_api.panels:
        await callback.answer("No servers configured", show_alert=True)
        return

    await state.set_state(BackupStates.waiting_for_server)
    await callback.message.edit_text(
        "🖥 <b>Select Server</b>\nWhich panel database do you want to download?",
        reply_markup=kb.server_selection_kb(panel_api.panels, "dl_server_"),
        parse_mode="HTML",
    )


@router.callback_query(BackupStates.waiting_for_server, F.data.startswith("dl_server_"))
async def admin_backup_dl_panel_process(callback: types.CallbackQuery, state: FSMContext):
    try:
        idx = int(callback.data.split("_")[2])
        target_panel = panel_api.panels[idx]
    except (ValueError, IndexError):
        await callback.answer("Error selecting server")
        return

    await callback.answer(f"Downloading from {target_panel.name}...", show_alert=False)

    content = await target_panel.request("GET", "backup", timeout=300)
    if content and isinstance(content, bytes):
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")

        await callback.message.answer_document(
            BufferedInputFile(content, filename=panel_backup_filename(target_panel, date_str)),
            caption=f"🎛 <b>{h(target_panel.name)}</b> Backup",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer("❌ Failed to download backup from server.")

    await state.clear()
    await admin_backups_menu(callback)


@router.callback_query(F.data == "admin_force_backup")
async def force_full_backup(callback: types.CallbackQuery):
    await callback.answer("Starting backup process...", show_alert=False)

    status_msg = await callback.message.answer("⏳ <b>Generating and sending backups...</b>", parse_mode="HTML")

    try:
        await send_backup(callback.bot)

        await status_msg.edit_text(
            "✅ <b>Backups successfully sent!</b>\nCheck the backup group or private messages.",
            parse_mode="HTML",
        )

        await asyncio.sleep(5)
        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Error during backup:</b> {h(e)}", parse_mode="HTML")


@router.callback_query(F.data == "admin_backup_restore")
async def admin_restore_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(RestoreStates.waiting_for_type)
    await callback.message.edit_text(
        "⚠️ <b>Restoration Mode</b>\n\nWhich database do you want to restore?\n<i>This will overwrite current data!</i>",
        reply_markup=kb.admin_restore_type_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_cancel_restore")
async def cancel_restore(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await admin_backups_menu(callback)


@router.callback_query(RestoreStates.waiting_for_type, F.data.startswith("restore_type_"))
async def restore_type_selected(callback: types.CallbackQuery, state: FSMContext):
    restore_type = callback.data.split("_")[2]
    await state.update_data(restore_type=restore_type)

    if restore_type == "panel":
        await state.set_state(RestoreStates.waiting_for_server)
        await callback.message.edit_text(
            "🖥 <b>Select Target Server</b>\nWhere should we restore the database?",
            reply_markup=kb.server_selection_kb(panel_api.panels, "rest_server_"),
            parse_mode="HTML",
        )
    else:
        await state.set_state(RestoreStates.waiting_for_file)
        await callback.message.edit_text(
            "📂 <b>Upload File</b>\n\nPlease upload the <code>.db</code> file for <b>BOT</b>.\nSend it as a Document.",
            reply_markup=kb.admin_back_kb(),
            parse_mode="HTML",
        )


@router.callback_query(RestoreStates.waiting_for_server, F.data.startswith("rest_server_"))
async def restore_panel_server_selected(callback: types.CallbackQuery, state: FSMContext):
    try:
        idx = int(callback.data.split("_")[2])
        target_panel_name = panel_api.panels[idx].name
    except (ValueError, IndexError):
        await callback.answer("Selection error")
        return

    await state.update_data(server_idx=idx)
    await state.set_state(RestoreStates.waiting_for_file)

    await callback.message.edit_text(
        f"📂 <b>Upload File</b>\n\n"
        f"Target: <b>{h(target_panel_name)}</b>\n"
        f"Please upload the <code>.db</code> file.\n"
        "Send it as a Document.",
        reply_markup=kb.admin_back_kb(),
        parse_mode="HTML",
    )


@router.message(RestoreStates.waiting_for_file, F.document)
async def process_restore_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    restore_type = data.get("restore_type")
    filename = (message.document.file_name or "").lower()
    file_size = int(message.document.file_size or 0)

    if not (filename.endswith(".db") or filename.endswith(".sqlite") or filename.endswith(".sqlite3")):
        await message.answer("⚠️ Please upload a file with .db extension.")
        return
    if file_size > MAX_RESTORE_FILE_BYTES:
        await message.answer("⚠️ File is too large (max 50 MB).")
        return

    wait_msg = await message.answer("⏳ Processing restoration...")

    file = await message.bot.get_file(message.document.file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    content = file_bytes.read()
    validation_error = _validate_sqlite_bytes(content)
    if validation_error:
        await wait_msg.delete()
        await message.answer(f"⚠️ {validation_error}")
        return

    if restore_type == "bot":
        try:
            db_path = "db/bot.db"
            if os.path.exists(db_path):
                os.rename(db_path, f"{db_path}.bak")
            with open(db_path, "wb") as f:
                f.write(content)
            await wait_msg.delete()
            await message.answer(
                "✅ <b>Bot Database Restored!</b>\nBot is restarting...",
                parse_mode="HTML",
            )
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            await wait_msg.edit_text(f"❌ Error restoring bot DB: {e}")

    elif restore_type == "panel":
        try:
            idx = data.get("server_idx")
            if idx is None:
                raise Exception("Server index lost")
            target_panel = panel_api.panels[idx]

            import aiohttp

            form = aiohttp.FormData()
            form.add_field(
                "file",
                content,
                filename="restore.db",
                content_type="application/x-sqlite3",
            )

            res = await target_panel.request("POST", "restore", data=form)

            await wait_msg.delete()
            if res and isinstance(res, dict) and res.get("status") == "restored":
                await message.answer(
                    f"✅ <b>Database Restored on {h(target_panel.name)}!</b>\nThe backend is restarting.",
                    parse_mode="HTML",
                )
            else:
                reason = str(res.get("error")) if isinstance(res, dict) and res.get("error") else "unknown error"
                await message.answer(
                    f"❌ Server {h(target_panel.name)} returned error: {h(reason)}.",
                    parse_mode="HTML",
                )
        except Exception as e:
            await wait_msg.edit_text(f"❌ Error uploading to panel: {e}")

    await state.clear()


@router.callback_query(F.data == "admin_system")
async def admin_system(callback: types.CallbackQuery):
    stats_list = await panel_api.get_system_stats_all()
    if not stats_list:
        await callback.answer("⚠️ Connection Failed!", show_alert=True)
        return

    text = "<b>🖥 System Resources</b>\n\n"
    for s in stats_list:
        if s.get("error"):
            text += f"❌ <b>{h(s['server_name'])}</b>: Offline\n\n"
        else:
            text += (
                f"✅ <b>{h(s['server_name'])}</b>\n"
                f"🧠 CPU: {s.get('cpu', 0)}%\n"
                f"💾 RAM: {s.get('mem_used', 0)}GB / {s.get('mem_total', 0)}GB ({s.get('mem_percent', 0)}%)\n\n"
            )

    await callback.message.edit_text(text, reply_markup=kb.admin_back_kb(), parse_mode="HTML")


@router.callback_query(F.data == "admin_restart_menu")
async def admin_restart_menu(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🔄 <b>Restart Manager</b>\nSelect target:",
        reply_markup=kb.server_selection_kb(panel_api.panels, "ask_restart_", include_all=True),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("ask_restart_"))
async def admin_ask_restart(callback: types.CallbackQuery):
    target = callback.data.split("_")[2]
    if target == "all":
        target_name = "ALL SERVERS"
    else:
        panel_idx = _parse_callback_int(callback.data)
        if panel_idx is None or not (0 <= panel_idx < len(panel_api.panels)):
            await callback.answer("Invalid target", show_alert=True)
            return
        target_name = panel_api.panels[panel_idx].name

    await callback.message.edit_text(
        f"⚠️ <b>Confirm Restart</b>\n\nTarget: <b>{h(target_name)}</b>\nConnections will drop briefly.",
        reply_markup=kb.confirm_restart_kb(target),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("confirm_restart_"))
async def admin_do_restart(callback: types.CallbackQuery):
    target = callback.data.split("_")[2]
    await callback.message.edit_text("🔄 Sending command...", parse_mode="HTML")

    if target == "all":
        await panel_api.restart_all()
        msg = "✅ All nodes restarted."
    else:
        panel_idx = _parse_callback_int(callback.data)
        if panel_idx is None or not (0 <= panel_idx < len(panel_api.panels)):
            await callback.answer("Invalid target", show_alert=True)
            await admin_restart_menu(callback)
            return
        await panel_api.restart_single(panel_idx)
        msg = "✅ Node restarted."

    await callback.message.edit_text(msg, reply_markup=kb.admin_back_kb(), parse_mode="HTML")


@router.callback_query(F.data == "admin_force_sync")
async def force_sync_handler(callback: types.CallbackQuery):
    await callback.answer("Starting synchronization...")

    status_msg = await callback.message.answer("⏳ <b>Synchronizing users across panels...</b>", parse_mode="HTML")

    try:
        await sync_users_across_panels(callback.bot)

        await status_msg.edit_text("✅ <b>Synchronization completed successfully!</b>", parse_mode="HTML")

        await asyncio.sleep(3)
        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Synchronization error:</b> {h(e)}", parse_mode="HTML")


@router.callback_query(F.data == "admin_users")
async def list_users(callback: types.CallbackQuery):
    users = db.get_all_users()
    if not users:
        await callback.message.edit_text(
            "📂 No users in database.",
            reply_markup=kb.admin_back_kb(),
            parse_mode="HTML",
        )
        return
    await callback.message.edit_text(
        "📂 <b>User Directory</b>",
        reply_markup=kb.user_list_kb(users, 0),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("users_page_"))
async def paginate_users(callback: types.CallbackQuery):
    page = _parse_callback_int(callback.data)
    if page is None or page < 0:
        await callback.answer("Invalid page")
        return
    users = db.get_all_users()
    await callback.message.edit_reply_markup(reply_markup=kb.user_list_kb(users, page))


@router.callback_query(F.data.startswith("manage_user_"))
async def manage_user_callback(callback: types.CallbackQuery, state: FSMContext):
    try:
        db_id = int(callback.data.split("_")[2])
        await manage_user(callback, state, override_db_id=db_id)
    except (ValueError, IndexError):
        await callback.answer("Invalid data")


async def manage_user(callback: types.CallbackQuery, state: FSMContext, override_db_id: int = None):
    await state.clear()

    db_id = override_db_id
    if db_id is None:
        try:
            db_id = int(callback.data.split("_")[2])
        except (ValueError, IndexError):
            await callback.answer("Error parsing ID")
            return

    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await callback.answer("Key not found in DB (deleted?)", show_alert=True)
        await list_users(callback)
        return

    tg_id = user_record[1]
    panel_email = user_record[3]
    inbound_tag = user_record[4]

    sync_errors = db.get_errors(tg_id)
    stats = await safe_get_stats(panel_email, inbound_tag=inbound_tag)

    if not stats:
        limit_str = "Unknown"
        expiry_str = "Unknown"
        status_icon = "❓ Unknown/Error"
        total_used = "N/A"
        is_enabled = False
        server_stats_text = "⚠️ <b>Cannot fetch live stats.</b> (Inbound mismatch?)\n"
    else:
        limit_str = format_bytes(stats["limit"]) if stats["limit"] > 0 else "♾️ Unlimited"
        total_used = format_bytes(stats["total"])

        expiry_str = "♾️ Never"
        if stats["expiry"] > 0:
            dt = datetime.datetime.fromtimestamp(stats["expiry"] / 1000)
            expiry_str = dt.strftime("%Y-%m-%d")
            if stats["expiry"] < (time.time() * 1000):
                expiry_str += " (EXPIRED)"

        status_icon = "🟢 Active" if stats["enable"] else "🔴 Disabled"
        is_enabled = stats["enable"]

        server_stats_text = ""
        if "per_server" in stats:
            for s in stats["per_server"]:
                server_stats_text += f"   • {h(s['name'])}: ⬆️{format_bytes(s['up'])} ⬇️{format_bytes(s['down'])}\n"

    error_text = ""
    if sync_errors:
        error_text = "\n⚠️ <b>SYNC ERRORS DETECTED:</b>\n"
        for server, msg in sync_errors:
            error_text += f"❌ <b>{h(server)}</b>: {h(msg)}\n"
        error_text += "<i>Fix config.yaml, restart bot, then click Retry.</i>\n"

    tg_username_display = f"@{h(user_record[2])}" if user_record[2] else "Not set"

    text = (
        f"👤 <b>Key Management</b>\n\n"
        f"🔑 <b>Key Name:</b> {h(panel_email)}\n"
        f"🌐 <b>Inbound:</b> {h(inbound_tag)}\n"
        f"🆔 <b>Owner ID:</b> <code>{tg_id}</code>\n"
        f"🏷 <b>Username:</b> {tg_username_display}\n"
        f"-------------------\n"
        f"📊 <b>Traffic:</b> {total_used} / {limit_str}\n"
        f"{server_stats_text}"
        f"📅 <b>Expires:</b> {expiry_str}\n"
        f"🔋 <b>Status:</b> {status_icon}\n"
        f"{error_text}"
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.user_manage_actions_kb(db_id, is_enabled, sync_errors),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            pass
        else:
            raise e


@router.callback_query(F.data.startswith("retry_sync_"))
async def retry_server_sync(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    db_id = int(parts[2])
    server_name = "_".join(parts[3:])

    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await callback.answer("Key gone", show_alert=True)
        return

    inbound_tag = user_record[4]
    stats = await safe_get_stats(user_record[3], inbound_tag=inbound_tag)
    limit_bytes = stats["limit"] if stats else 0
    expiry_time = stats["expiry"] if stats else 0

    await callback.answer(f"Retrying {server_name}...", show_alert=False)

    result = await panel_api.add_client_single(
        server_name,
        user_record[3],
        limit_bytes=limit_bytes,
        expiry_time=expiry_time,
        user_id=user_record[5],
        inbound_tag=inbound_tag,
    )

    if result["success"]:
        db.remove_error(user_record[1], server_name)
        await callback.answer("✅ Success! Error cleared.", show_alert=True)
        await manage_user(callback, state, override_db_id=db_id)
    else:
        err_msg = result.get("error", "Unknown")
        db.add_error(user_record[1], server_name, err_msg)
        await callback.answer(f"❌ Failed: {err_msg}", show_alert=True)
        await manage_user(callback, state, override_db_id=db_id)


@router.callback_query(F.data.startswith("toggle_enable_"))
async def toggle_enable_user(callback: types.CallbackQuery, state: FSMContext):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        return

    inbound_tag = user_record[4]
    stats = await safe_get_stats(user_record[3], inbound_tag=inbound_tag)
    if not stats:
        await callback.answer("❌ Server error.", show_alert=True)
        return

    new_state = not stats["enable"]

    updated = await panel_api.update_client_all(user_record[3], {"enable": new_state}, inbound_tag=inbound_tag)
    if not updated:
        await callback.answer("⚠️ Update failed on one or more servers.", show_alert=True)
        return

    msg = "Key Enabled" if new_state else "Key Disabled"
    await callback.answer(msg)
    await manage_user(callback, state, override_db_id=db_id)


@router.callback_query(F.data.startswith("ask_reset_"))
async def ask_reset_traffic(callback: types.CallbackQuery):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    await callback.message.edit_text(
        "⚠️ <b>Confirm Reset</b>\n\nReset usage for this key on ALL servers?",
        reply_markup=kb.confirm_reset_traffic_kb(db_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("confirm_reset_"))
async def confirm_reset_traffic(callback: types.CallbackQuery, state: FSMContext):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    user_record = db.get_user_by_db_id(db_id)
    if user_record:
        reset_ok = await panel_api.reset_traffic_all(user_record[3], inbound_tag=user_record[4])
        if not reset_ok:
            await callback.answer("⚠️ Reset failed on one or more servers.", show_alert=True)
            return
        await callback.answer("✅ Traffic reset globally", show_alert=True)
        await manage_user(callback, state, override_db_id=db_id)


@router.callback_query(F.data.startswith("ask_delete_"))
async def ask_delete_user(callback: types.CallbackQuery):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    await callback.message.edit_text(
        "⚠️ <b>Delete Confirmation</b>\n\nAre you sure? This removes THIS key from ALL servers.",
        reply_markup=kb.confirm_delete_kb(db_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_user(callback: types.CallbackQuery):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    user_record = db.get_user_by_db_id(db_id)
    if user_record:
        deleted = await panel_api.delete_client_all(user_record[3], inbound_tag=user_record[4])
        if not deleted:
            await callback.answer("⚠️ Delete failed on one or more servers.", show_alert=True)
            return
        db.delete_user_record(db_id)
        await callback.answer("Key deleted")
        await list_users(callback)
    else:
        await callback.answer("Key not found")
        await list_users(callback)


@router.callback_query(F.data.startswith("edit_limit_"))
async def ask_new_limit(callback: types.CallbackQuery, state: FSMContext):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    await state.update_data(target_db_id=db_id)
    await state.set_state(EditUserStates.waiting_for_new_limit)

    await callback.message.edit_text(
        "📊 <b>Edit Data Limit</b>\n\nEnter limit in <b>GB</b>.\nThis limit will be set on EACH server.",
        reply_markup=kb.edit_limit_kb(db_id),
        parse_mode="HTML",
    )


@router.message(EditUserStates.waiting_for_new_limit)
async def process_new_limit(message: types.Message, state: FSMContext):
    data = await state.get_data()
    db_id = data.get("target_db_id")
    text = _message_text(message)

    if not db_id:
        await message.answer("Session expired.")
        await state.clear()
        return

    async def safe_return():
        fake_cb = types.CallbackQuery(
            id="0",
            from_user=message.from_user,
            chat_instance="0",
            message=message,
            data="",
        )
        await manage_user(fake_cb, state, override_db_id=db_id)

    if not text:
        await message.answer("⚠️ Text value required.")
        return

    if text.lower() in ["cancel", "отмена"]:
        await safe_return()
        return

    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await message.answer("Key not found.")
        await state.clear()
        return
    try:
        gb = float(text.replace(",", "."))
        bytes_val = int(gb * 1024 * 1024 * 1024)
    except ValueError:
        await message.answer("⚠️ Numbers only.")
        return

    updated = await panel_api.update_client_all(user_record[3], {"limit_bytes": bytes_val}, inbound_tag=user_record[4])
    if not updated:
        await message.answer("❌ Failed to update limit on one or more servers.")
        return
    await message.answer(f"✅ Limit set to {gb} GB.")
    await safe_return()


@router.callback_query(EditUserStates.waiting_for_new_limit, F.data == "skip_step")
async def process_limit_unlimited_cb(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    db_id = data.get("target_db_id")
    if not db_id:
        await callback.answer("Session expired", show_alert=True)
        await state.clear()
        return
    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await callback.answer("Key not found", show_alert=True)
        await state.clear()
        return

    updated = await panel_api.update_client_all(user_record[3], {"limit_bytes": 0}, inbound_tag=user_record[4])
    if not updated:
        await callback.answer("⚠️ Update failed on one or more servers.", show_alert=True)
        return
    await callback.answer("Limit removed")
    await manage_user(callback, state, override_db_id=db_id)


@router.callback_query(F.data.startswith("edit_expiry_"))
async def show_expiry_options(callback: types.CallbackQuery, state: FSMContext):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    await state.update_data(target_db_id=db_id)
    await callback.message.edit_text(
        "📅 <b>Edit Expiry</b>",
        reply_markup=kb.expiry_edit_options_kb(db_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("extend_30_"))
async def extend_expiry_30(callback: types.CallbackQuery, state: FSMContext):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await callback.answer("Key not found", show_alert=True)
        return

    stats = await safe_get_stats(user_record[3], inbound_tag=user_record[4])
    current_ts = stats["expiry"] if stats else 0
    now_ts = int(time.time() * 1000)
    base_time = max(current_ts, now_ts) if current_ts > 0 else now_ts
    new_time = base_time + (30 * 24 * 60 * 60 * 1000)

    updated = await panel_api.update_client_all(
        user_record[3],
        {"expiry_time": new_time, "enable": True},
        inbound_tag=user_record[4],
    )
    if not updated:
        await callback.answer("⚠️ Update failed on one or more servers.", show_alert=True)
        return
    await callback.answer("Extended by 30 days")
    await manage_user(callback, state, override_db_id=db_id)


@router.callback_query(F.data.startswith("make_permanent_"))
async def make_expiry_permanent(callback: types.CallbackQuery, state: FSMContext):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await callback.answer("Key not found", show_alert=True)
        return

    updated = await panel_api.update_client_all(
        user_record[3],
        {"expiry_time": 0, "enable": True},
        inbound_tag=user_record[4],
    )
    if not updated:
        await callback.answer("⚠️ Update failed on one or more servers.", show_alert=True)
        return
    await callback.answer("Set to Permanent")
    await manage_user(callback, state, override_db_id=db_id)


@router.callback_query(F.data.startswith("custom_days_"))
async def ask_custom_days(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(EditUserStates.waiting_for_days_to_extend)
    await callback.message.edit_text("📅 Enter days from NOW (or 'cancel'):", parse_mode="HTML")


@router.message(EditUserStates.waiting_for_days_to_extend)
async def process_custom_days(message: types.Message, state: FSMContext):
    data = await state.get_data()
    db_id = data.get("target_db_id")
    text = _message_text(message)

    if not db_id:
        await message.answer("Session expired.")
        await state.clear()
        return

    async def safe_return():
        fake_cb = types.CallbackQuery(
            id="0",
            from_user=message.from_user,
            chat_instance="0",
            message=message,
            data="",
        )
        await manage_user(fake_cb, state, override_db_id=db_id)

    if not text:
        await message.answer("⚠️ Text value required.")
        return

    if text.lower() in ["cancel", "отмена", "back"]:
        await safe_return()
        return

    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await message.answer("Key not found.")
        await state.clear()
        return
    try:
        days = int(text)
        new_time = int(time.time() * 1000) + (days * 24 * 60 * 60 * 1000)
    except (TypeError, ValueError):
        await message.answer("Number only.")
        return

    updated = await panel_api.update_client_all(
        user_record[3],
        {"expiry_time": new_time, "enable": True},
        inbound_tag=user_record[4],
    )
    if not updated:
        await message.answer("❌ Failed to update expiry on one or more servers.")
        return
    await safe_return()


@router.callback_query(F.data.startswith("edit_username_"))
async def edit_username_start(callback: types.CallbackQuery, state: FSMContext):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    await state.update_data(target_db_id=db_id)
    await state.set_state(EditUserStates.waiting_for_new_username)

    await callback.message.edit_text(
        "✏️ <b>Edit Username</b>\n\nEnter new Username (without @). This affects ALL keys of this user.",
        reply_markup=kb.edit_username_kb(db_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("clear_username_"))
async def process_clear_username(callback: types.CallbackQuery, state: FSMContext):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await callback.answer("Key not found", show_alert=True)
        return

    db.update_username(user_record[1], None)

    await callback.answer("✅ Username cleared")
    await manage_user(callback, state, override_db_id=db_id)


@router.message(EditUserStates.waiting_for_new_username)
async def edit_username_process(message: types.Message, state: FSMContext):
    data = await state.get_data()
    db_id = data.get("target_db_id")
    text = _message_text(message)

    if not db_id:
        await message.answer("Session expired.")
        await state.clear()
        return
    if not text:
        await message.answer("⚠️ Text value required.")
        return

    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await message.answer("Key not found.")
        await state.clear()
        return

    async def safe_return():
        fake_cb = types.CallbackQuery(
            id="0",
            from_user=message.from_user,
            chat_instance="0",
            message=message,
            data="",
        )
        await manage_user(fake_cb, state, override_db_id=db_id)

    if text.lower() in ["cancel", "отмена"]:
        await safe_return()
        return

    new_username = None
    if text.lower() != "clear":
        new_username = text.replace("@", "")
        if not USERNAME_RE.fullmatch(new_username):
            await message.answer("⚠️ Username must be 3-32 chars: letters, numbers, underscore.")
            return

    db.update_username(user_record[1], new_username)

    await message.answer("✅ Username updated.")
    await safe_return()


@router.callback_query(F.data.startswith("edit_tgid_"))
async def edit_tgid_start(callback: types.CallbackQuery, state: FSMContext):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await callback.answer("Key not found", show_alert=True)
        return

    await state.update_data(target_db_id=db_id)
    await state.set_state(EditUserStates.waiting_for_new_tg_id)

    await callback.message.edit_text(
        f"🆔 <b>Transfer Key</b>\n\n"
        f"Current Owner ID: <code>{user_record[1]}</code>\n"
        "Enter new numeric Telegram ID to transfer this key to:",
        reply_markup=kb.edit_cancel_kb(db_id),
        parse_mode="HTML",
    )


@router.message(EditUserStates.waiting_for_new_tg_id)
async def edit_tgid_process(message: types.Message, state: FSMContext):
    data = await state.get_data()
    db_id = data.get("target_db_id")
    text = _message_text(message)

    if not db_id:
        await message.answer("Session expired.")
        await state.clear()
        return
    if not text:
        await message.answer("⚠️ Text value required.")
        return

    if text.lower() in ["cancel", "отмена"]:
        fake_cb = types.CallbackQuery(
            id="0",
            from_user=message.from_user,
            chat_instance="0",
            message=message,
            data="",
        )
        await manage_user(fake_cb, state, override_db_id=db_id)
        return

    if not text.isdigit():
        await message.answer("⚠️ Please enter a valid numeric ID.")
        return

    new_id = int(text)

    db.update_telegram_id_for_record(db_id, new_id)

    await message.answer(f"✅ Key transferred to ID {new_id}.")
    fake_cb = types.CallbackQuery(
        id="0",
        from_user=message.from_user,
        chat_instance="0",
        message=message,
        data="",
    )
    await manage_user(fake_cb, state, override_db_id=db_id)


@router.callback_query(F.data.startswith("edit_keyname_"))
async def edit_keyname_start(callback: types.CallbackQuery, state: FSMContext):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await callback.answer("Key not found", show_alert=True)
        return

    await state.update_data(target_db_id=db_id, current_email=user_record[3])
    await state.set_state(EditUserStates.waiting_for_new_key_name)

    await callback.message.edit_text(
        f"✏️ <b>Edit Key Name (Email)</b>\n\n"
        f"Current Name: <code>{h(user_record[3])}</code>\n\n"
        "Enter the NEW Name (Email).\n"
        "⚠️ <i>This will update config on all servers.</i>",
        reply_markup=kb.edit_cancel_kb(db_id),
        parse_mode="HTML",
    )


@router.message(EditUserStates.waiting_for_new_key_name)
async def edit_keyname_process(message: types.Message, state: FSMContext):
    data = await state.get_data()
    db_id = data.get("target_db_id")
    old_email = data.get("current_email")
    new_name = _message_text(message)

    if not db_id or not old_email:
        await message.answer("Session expired.")
        await state.clear()
        return

    async def safe_return():
        fake_cb = types.CallbackQuery(
            id="0",
            from_user=message.from_user,
            chat_instance="0",
            message=message,
            data="",
        )
        await manage_user(fake_cb, state, override_db_id=db_id)

    if new_name.lower() in ["cancel", "отмена"]:
        await safe_return()
        return

    if not new_name:
        await message.answer("Name cannot be empty.")
        return
    if not EMAIL_RE.fullmatch(new_name):
        await message.answer("⚠️ Invalid key name format.")
        return

    wait_msg = await message.answer("🔄 Updating name on servers...")

    user_record = db.get_user_by_db_id(db_id)
    if not user_record:
        await wait_msg.delete()
        await message.answer("Key not found.")
        await state.clear()
        return

    updated = await panel_api.update_client_all(old_email, {"new_email": new_name}, inbound_tag=user_record[4])
    if not updated:
        await wait_msg.delete()
        await message.answer("❌ Failed to rename key on one or more servers.")
        return

    db.update_panel_email(db_id, new_name)

    await wait_msg.delete()
    await message.answer(f"✅ Key renamed to: {new_name}")
    await safe_return()


@router.callback_query(F.data.startswith("cancel_edit_"))
async def process_cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    db_id = _parse_callback_int(callback.data)
    if db_id is None:
        await callback.answer("Invalid key", show_alert=True)
        return
    await callback.answer("Cancelled")
    await manage_user(callback, state, override_db_id=db_id)


@router.callback_query(F.data == "admin_add_user")
async def add_user_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddUserStates.waiting_for_tg_id)
    await callback.message.edit_text(
        "➕ <b>New User</b>\nEnter numeric <b>Telegram ID</b>:",
        reply_markup=kb.admin_back_kb(),
        parse_mode="HTML",
    )


@router.message(AddUserStates.waiting_for_tg_id)
async def process_tg_id(message: types.Message, state: FSMContext):
    text = _message_text(message)
    if not text:
        await message.answer("⚠️ Numeric ID required.")
        return
    if not text.isdigit():
        await message.answer("⚠️ Invalid ID.")
        return
    tg_id = int(text)
    if db.get_users_by_tg_id(tg_id):
        await message.answer("⚠️ User already exists.", reply_markup=kb.admin_back_kb())
        await state.clear()
        return
    await state.update_data(tg_id=tg_id)
    await state.set_state(AddUserStates.waiting_for_username)

    await message.answer(
        "👤 <b>Username</b> (enter text or press Skip):", parse_mode="HTML", reply_markup=kb.skip_username_kb()
    )


@router.callback_query(AddUserStates.waiting_for_username, F.data == "skip_username")
async def skip_username_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(username=None)

    await state.set_state(AddUserStates.waiting_for_email)

    await callback.message.edit_text("👤 <b>Username:</b> <i>Skipped</i>", parse_mode="HTML")
    await callback.message.answer("📧 <b>Unique Email/ID</b>:", parse_mode="HTML", reply_markup=kb.admin_back_kb())


@router.message(AddUserStates.waiting_for_username)
async def process_username(message: types.Message, state: FSMContext):
    username = _message_text(message)
    if not username:
        await message.answer("⚠️ Username is required or press Skip.")
        return
    if username.lower() in ["-", "skip", "no"]:
        username = None
    else:
        username = username.replace("@", "")
        if not USERNAME_RE.fullmatch(username):
            await message.answer("⚠️ Username must be 3-32 chars: letters, numbers, underscore.")
            return
    await state.update_data(username=username)
    await state.set_state(AddUserStates.waiting_for_email)
    await message.answer("📧 <b>Unique Email/ID</b>:", parse_mode="HTML")


@router.message(AddUserStates.waiting_for_email)
async def process_email(message: types.Message, state: FSMContext):
    email = _message_text(message)
    if not email:
        await message.answer("⚠️ Email/ID is required.")
        return
    if not EMAIL_RE.fullmatch(email):
        await message.answer("⚠️ Invalid Email/ID format.")
        return
    await state.update_data(email=email)
    await state.set_state(AddUserStates.waiting_for_limit_gb)
    await message.answer(
        "📊 <b>Data Limit (GB)</b>\nEnter number or skip for Unlimited:",
        reply_markup=kb.skip_kb(),
        parse_mode="HTML",
    )


@router.callback_query(AddUserStates.waiting_for_limit_gb, F.data == "skip_step")
async def skip_limit_gb(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(limit_bytes=0)
    await state.set_state(AddUserStates.waiting_for_expiry_days)
    await callback.message.edit_text(
        "📅 <b>Expiry (Days)</b>\nEnter days or skip for Permanent:",
        reply_markup=kb.skip_kb(),
        parse_mode="HTML",
    )


@router.message(AddUserStates.waiting_for_limit_gb)
async def process_limit_gb(message: types.Message, state: FSMContext):
    text = _message_text(message)
    if not text:
        await message.answer("Numbers only.")
        return
    try:
        gb = float(text.replace(",", "."))
        bytes_val = int(gb * 1024 * 1024 * 1024)
        await state.update_data(limit_bytes=bytes_val)
    except (AttributeError, TypeError, ValueError):
        await message.answer("Numbers only.")
        return
    await state.set_state(AddUserStates.waiting_for_expiry_days)
    await message.answer(
        "📅 <b>Expiry (Days)</b>\nEnter days or skip for Permanent:",
        reply_markup=kb.skip_kb(),
        parse_mode="HTML",
    )


@router.callback_query(AddUserStates.waiting_for_expiry_days, F.data == "skip_step")
async def skip_expiry(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await create_user_final(callback.message, state, 0, callback.bot)


@router.message(AddUserStates.waiting_for_expiry_days)
async def process_expiry(message: types.Message, state: FSMContext):
    text = _message_text(message)
    if not text:
        await message.answer("Whole numbers only.")
        return
    try:
        days = int(text)
        expiry_ts = int(time.time() * 1000) + (days * 24 * 60 * 60 * 1000)
    except (TypeError, ValueError):
        await message.answer("Whole numbers only.")
        return
    await create_user_final(message, state, expiry_ts, message.bot)


async def create_user_final(message, state, expiry_ts, bot):
    data = await state.get_data()
    limit_bytes = data.get("limit_bytes", 0)
    wait_msg = await message.answer("⏳ <b>Deploying User...</b>\nContacting servers...", parse_mode="HTML")
    report = await panel_api.add_client_all(data["email"], limit_bytes=limit_bytes, expiry_time=expiry_ts)
    await wait_msg.delete()

    success_list = report["success"]
    failed_list = report["failed"]
    skipped_list = report.get("skipped", [])

    if not success_list and not failed_list and not skipped_list:
        await message.answer("❌ No servers configured.", reply_markup=kb.admin_main_kb())
        await state.clear()
        return

    if not success_list:
        status_text = "\n".join([f"❌ <b>{h(f['name'])}</b>: <i>{h(f['reason'])}</i>" for f in failed_list])
        await message.answer(
            f"❌ <b>Deployment failed on all servers.</b>\nUser was <b>not</b> saved in bot database.\n\n{status_text}",
            parse_mode="HTML",
            reply_markup=kb.admin_main_kb(),
        )
        await state.clear()
        return

    primary_uuid = success_list[0]["uuid"] if success_list else str(uuid.uuid4())
    inbound_tags = {str(item.get("inbound_tag") or "").strip() for item in success_list if item}
    inbound_tags = {item for item in inbound_tags if item}
    stored_inbound_tag = "multi"
    if not failed_list and len(inbound_tags) == 1:
        stored_inbound_tag = next(iter(inbound_tags))
    db.add_user(
        data["tg_id"],
        data["username"],
        data["email"],
        stored_inbound_tag,
        primary_uuid,
    )

    if success_list:
        try:
            await bot.send_message(
                data["tg_id"],
                "🎉 <b>Your account is ready!</b>\nType /start to get access.",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Failed to send onboarding message to %s: %s", data["tg_id"], exc)
    if failed_list:
        for fail in failed_list:
            db.add_error(data["tg_id"], fail["name"], fail["reason"])

    limit_txt = "♾️ Unlimited" if limit_bytes == 0 else f"{format_bytes(limit_bytes)}"
    expiry_txt = (
        "♾️ Forever" if expiry_ts == 0 else datetime.datetime.fromtimestamp(expiry_ts / 1000).strftime("%Y-%m-%d")
    )

    status_text = ""
    if success_list:
        status_text += "\n".join([f"✅ <b>{h(s['name'])}</b>" for s in success_list])
    if failed_list:
        if status_text:
            status_text += "\n"
        status_text += "\n".join([f"❌ <b>{h(f['name'])}</b>: <i>{h(f['reason'])}</i>" for f in failed_list])
    if skipped_list:
        if status_text:
            status_text += "\n"
        status_text += "\n".join([f"⊘ <b>{h(s['name'])}</b>: <i>{h(s['reason'])}</i>" for s in skipped_list])

    header_icon = "✅" if success_list else "⚠️"
    final_msg = (
        f"{header_icon} <b>Deployment Report</b>\n\n"
        f"👤 <b>User:</b> <code>{h(data['email'])}</code>\n"
        f"🆔 <b>TG ID:</b> <code>{data['tg_id']}</code>\n"
        f"📊 <b>Limit:</b> {limit_txt}\n"
        f"📅 <b>Expiry:</b> {expiry_txt}\n\n"
        f"<b>📡 Server Status:</b>\n"
        f"{status_text}\n\n"
        f"<i>Check User Management to retry failed servers.</i>"
    )
    await message.answer(final_msg, parse_mode="HTML", reply_markup=kb.admin_main_kb())
    await state.clear()


@router.callback_query(F.data == "admin_link_user")
async def link_user_start(callback: types.CallbackQuery, state: FSMContext):
    if not panel_api.panels:
        await callback.answer("No panels configured.", show_alert=True)
        return

    await state.set_state(LinkUserStates.waiting_for_server)
    await callback.message.edit_text(
        "🔗 <b>Link Existing User</b>\n\nConnect a Telegram ID to an existing client.\nSelect Source Server:",
        reply_markup=kb.server_selection_kb(panel_api.panels, "link_srv_"),
        parse_mode="HTML",
    )


@router.callback_query(LinkUserStates.waiting_for_server, F.data.startswith("link_srv_"))
async def link_process_server_select(callback: types.CallbackQuery, state: FSMContext):
    try:
        idx = int(callback.data.split("_")[2])
        target_panel = panel_api.panels[idx]
    except (ValueError, IndexError):
        await callback.answer("Error selecting server")
        return
    await state.update_data(server_idx=idx)
    inbounds = await target_panel.request("GET", "inbounds")
    if not isinstance(inbounds, list):
        await callback.message.edit_text(
            f"❌ Error fetching data from {target_panel.name}.",
            reply_markup=kb.admin_back_kb(),
        )
        await state.clear()
        return

    existing_keys = {(u[3], u[4]) for u in db.get_all_users()}

    available_clients = []
    for ib in inbounds:
        if ib.get("tag") != target_panel.target_inbound:
            continue
        for c in ib.get("settings", {}).get("clients", []):
            client_key = (c.get("email"), ib.get("tag"))
            if client_key not in existing_keys:
                available_clients.append({"email": c["email"], "id": c["id"], "tag": ib["tag"]})

    if not available_clients:
        await callback.message.edit_text(
            f"⚠️ No unlinked clients found on {target_panel.name}.",
            reply_markup=kb.admin_back_kb(),
        )
        await state.clear()
        return

    await state.update_data(available_clients=available_clients)
    await state.set_state(LinkUserStates.waiting_for_client)
    await callback.message.edit_text(
        f"📧 <b>Select Client ({h(target_panel.name)})</b>:",
        reply_markup=kb.client_list_kb(available_clients, 0),
        parse_mode="HTML",
    )


@router.callback_query(LinkUserStates.waiting_for_client, F.data.startswith("link_page_"))
async def link_process_client_pagination(callback: types.CallbackQuery, state: FSMContext):
    page = _parse_callback_int(callback.data)
    if page is None or page < 0:
        await callback.answer("Invalid page")
        return
    data = await state.get_data()
    clients = data.get("available_clients", [])
    await callback.message.edit_reply_markup(reply_markup=kb.client_list_kb(clients, page))


@router.callback_query(LinkUserStates.waiting_for_client, F.data.startswith("link_client_"))
async def link_process_client_select(callback: types.CallbackQuery, state: FSMContext):
    client_index = _parse_callback_int(callback.data)
    if client_index is None or client_index < 0:
        await callback.answer("Invalid client", show_alert=True)
        return

    data = await state.get_data()
    clients = data.get("available_clients", [])
    if client_index >= len(clients):
        await callback.answer("Client list expired", show_alert=True)
        return

    client = clients[client_index]

    if not client:
        await callback.answer("Client not found", show_alert=True)
        return

    await state.update_data(
        target_email=client["email"],
        target_uuid=client["id"],
        target_inbound_tag=client["tag"],
    )

    await state.set_state(LinkUserStates.waiting_for_tg_id)
    await callback.message.edit_text(
        f"🔗 Linking <b>{h(client['email'])}</b>\n\nEnter the numeric <b>Telegram ID</b> to assign this key to:",
        reply_markup=kb.admin_back_kb(),
        parse_mode="HTML",
    )


@router.message(LinkUserStates.waiting_for_tg_id)
async def link_process_tg_id(message: types.Message, state: FSMContext):
    text = _message_text(message)
    if not text:
        await message.answer("⚠️ Numbers only.")
        return
    if not text.isdigit():
        await message.answer("⚠️ Numbers only.")
        return

    tg_id = int(text)
    data = await state.get_data()
    email = data.get("target_email")
    uuid_val = data.get("target_uuid")
    inbound_tag = str(data.get("target_inbound_tag") or "multi").strip() or "multi"
    if not email or not uuid_val:
        await message.answer("Session expired.")
        await state.clear()
        return

    db.add_user(tg_id, None, email, inbound_tag, uuid_val)

    await message.answer(
        f"✅ <b>Successfully Linked!</b>\nKey: {h(email)}\nOwner: {tg_id}",
        parse_mode="HTML",
        reply_markup=kb.admin_main_kb(),
    )

    try:
        await message.bot.send_message(tg_id, "🔗 A key has been linked to your account.")
    except Exception as exc:
        logger.warning("Failed to notify linked user %s: %s", tg_id, exc)

    await state.clear()


@router.callback_query(F.data == "admin_cancel_add")
async def cancel_add(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await admin_home(callback, state)
