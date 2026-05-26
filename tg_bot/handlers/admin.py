import os
import logging
import sqlite3
import tempfile
from html import escape
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile
from api_service import panel_api
from runtime_config import runtime_config
from states import RestoreStates, BackupStates
import keyboards as kb
import datetime

router = Router()
logger = logging.getLogger(__name__)


def _is_admin(event) -> bool:
    """Live admin check — reads runtime_config every call so changes via the
    panel UI take effect on the next message without restarting the bot."""
    user = getattr(event, "from_user", None)
    if user is None or user.id is None:
        return False
    return user.id in runtime_config.admin_ids_set()


router.message.filter(_is_admin)
router.callback_query.filter(_is_admin)


def h(value):
    return escape(str(value), quote=True)


def _parse_callback_int(callback_data, index=2):
    try:
        return int(str(callback_data).split("_")[index])
    except (TypeError, ValueError, IndexError):
        return None


MAX_RESTORE_FILE_BYTES = 50 * 1024 * 1024
SQLITE_HEADER = b"SQLite format 3\x00"


def is_admin(user_id):
    return user_id in runtime_config.admin_ids_set()


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


@router.message(Command("admin"))
async def admin_start(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    alive_count = 0
    total_count = len(panel_api.panels)
    for p in panel_api.panels:
        try:
            if await p.health_check():
                alive_count += 1
        except Exception as exc:
            logger.debug("Panel health check failed for %s: %s", p.name, exc)

    linked = await panel_api.get_linked_panels()
    for lp in linked:
        if lp.get("enable", True):
            total_count += 1
            if lp.get("status") == "online":
                alive_count += 1

    status_text = f"✅ {alive_count}/{total_count} Online"

    await message.answer(
        f"🛠 <b>Administrator Dashboard</b>\nPanels: {status_text}",
        reply_markup=kb.admin_main_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_home")
async def admin_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()

    alive_count = 0
    total_count = len(panel_api.panels)
    for p in panel_api.panels:
        try:
            if await p.health_check():
                alive_count += 1
        except Exception:
            pass
    linked = await panel_api.get_linked_panels()
    for lp in linked:
        if lp.get("enable", True):
            total_count += 1
            if lp.get("status") == "online":
                alive_count += 1

    status_text = f"✅ {alive_count}/{total_count} Online"
    await callback.message.edit_text(
        f"🛠 <b>Administrator Dashboard</b>\nPanels: {status_text}",
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


@router.callback_query(F.data == "backup_dl_panel_menu")
async def admin_backup_dl_panel_menu(callback: types.CallbackQuery, state: FSMContext):
    if not panel_api.panels:
        await callback.answer("No servers configured", show_alert=True)
        return

    await state.set_state(BackupStates.waiting_for_server)
    linked = await panel_api.get_linked_panels()
    await callback.message.edit_text(
        "🖥 <b>Select Server</b>\nWhich panel database do you want to download?",
        reply_markup=kb.server_selection_kb(panel_api.panels, "dl_server_", linked_panels=linked),
        parse_mode="HTML",
    )


@router.callback_query(BackupStates.waiting_for_server, F.data.startswith("dl_server_"))
async def admin_backup_dl_panel_process(callback: types.CallbackQuery, state: FSMContext):
    raw = callback.data.removeprefix("dl_server_")

    if raw.startswith("lp_"):
        panel_id = raw.removeprefix("lp_")
        linked = await panel_api.get_linked_panels()
        lp = next((p for p in linked if str(p.get("id")) == panel_id), None)
        if not lp:
            await callback.answer("Panel not found", show_alert=True)
            return
        panel_name = lp.get("name", f"Panel {panel_id}")
        await callback.answer(f"Downloading from {panel_name}...", show_alert=False)
        content = await panel_api.panels[0].request("GET", f"panels/{panel_id}/backup", timeout=300)
    else:
        try:
            idx = int(raw)
            target_panel = panel_api.panels[idx]
        except (ValueError, IndexError):
            await callback.answer("Error selecting server")
            return
        panel_name = target_panel.name
        await callback.answer(f"Downloading from {panel_name}...", show_alert=False)
        content = await target_panel.request("GET", "backup", timeout=300)

    if content and isinstance(content, bytes):
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        filename = f"{panel_name}_{date_str}.db"
        await callback.message.answer_document(
            BufferedInputFile(content, filename=filename),
            caption=f"🎛 <b>{h(panel_name)}</b> Backup",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer("❌ Failed to download backup from server.")

    await state.clear()
    await admin_backups_menu(callback)


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
        linked = await panel_api.get_linked_panels()
        await callback.message.edit_text(
            "🖥 <b>Select Target Server</b>\nWhere should we restore the database?",
            reply_markup=kb.server_selection_kb(panel_api.panels, "rest_server_", linked_panels=linked),
            parse_mode="HTML",
        )
    else:
        await callback.answer("Only panel restore is supported.", show_alert=True)


@router.callback_query(RestoreStates.waiting_for_server, F.data.startswith("rest_server_"))
async def restore_panel_server_selected(callback: types.CallbackQuery, state: FSMContext):
    raw = callback.data.removeprefix("rest_server_")
    if raw.startswith("lp_"):
        panel_id = raw.removeprefix("lp_")
        linked = await panel_api.get_linked_panels()
        lp = next((p for p in linked if str(p.get("id")) == panel_id), None)
        if not lp:
            await callback.answer("Panel not found", show_alert=True)
            return
        target_panel_name = lp.get("name", f"Panel {panel_id}")
        await state.update_data(linked_panel_id=int(panel_id))
    else:
        try:
            idx = int(raw)
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

    if restore_type == "panel":
        try:
            linked_panel_id = data.get("linked_panel_id")
            if linked_panel_id:
                import aiohttp

                form = aiohttp.FormData()
                form.add_field("file", content, filename="restore.db", content_type="application/x-sqlite3")
                res = await panel_api.panels[0].request("POST", f"panels/{linked_panel_id}/restore", data=form)
                target_name = f"Linked Panel #{linked_panel_id}"
            else:
                idx = data.get("server_idx")
                if idx is None:
                    raise Exception("Server index lost")
                target_panel = panel_api.panels[idx]
                target_name = target_panel.name
                import aiohttp

                form = aiohttp.FormData()
                form.add_field("file", content, filename="restore.db", content_type="application/x-sqlite3")
                res = await target_panel.request("POST", "restore", data=form)

            await wait_msg.delete()
            if res and isinstance(res, dict) and res.get("status") == "restored":
                await message.answer(
                    f"✅ <b>Database Restored on {h(target_name)}!</b>\nThe backend is restarting.",
                    parse_mode="HTML",
                )
            else:
                reason = str(res.get("error")) if isinstance(res, dict) and res.get("error") else "unknown error"
                await message.answer(
                    f"❌ Server {h(target_name)} returned error: {h(reason)}.",
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
    linked = await panel_api.get_linked_panels()
    await callback.message.edit_text(
        "🔄 <b>Restart Manager</b>\nSelect target:",
        reply_markup=kb.server_selection_kb(panel_api.panels, "ask_restart_", include_all=True, linked_panels=linked),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("ask_restart_"))
async def admin_ask_restart(callback: types.CallbackQuery):
    raw = callback.data.removeprefix("ask_restart_")
    if raw == "all":
        target_name = "ALL SERVERS"
        confirm_data = "all"
    elif raw.startswith("lp_"):
        panel_id = raw.removeprefix("lp_")
        linked = await panel_api.get_linked_panels()
        lp = next((p for p in linked if str(p.get("id")) == panel_id), None)
        if not lp:
            await callback.answer("Panel not found", show_alert=True)
            return
        target_name = lp.get("name", f"Panel {panel_id}")
        confirm_data = f"lp_{panel_id}"
    else:
        panel_idx = _parse_callback_int(callback.data)
        if panel_idx is None or not (0 <= panel_idx < len(panel_api.panels)):
            await callback.answer("Invalid target", show_alert=True)
            return
        target_name = panel_api.panels[panel_idx].name
        confirm_data = str(panel_idx)

    await callback.message.edit_text(
        f"⚠️ <b>Confirm Restart</b>\n\nTarget: <b>{h(target_name)}</b>\nConnections will drop briefly.",
        reply_markup=kb.confirm_restart_kb(confirm_data),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("confirm_restart_"))
async def admin_do_restart(callback: types.CallbackQuery):
    raw = callback.data.removeprefix("confirm_restart_")
    await callback.message.edit_text("🔄 Sending command...", parse_mode="HTML")

    if raw == "all":
        await panel_api.restart_all()
        msg = "✅ All panels restarted."
    elif raw.startswith("lp_"):
        panel_id = int(raw.removeprefix("lp_"))
        await panel_api.restart_linked_panel(panel_id)
        msg = "✅ Linked panel restarted."
    else:
        panel_idx = int(raw)
        if not (0 <= panel_idx < len(panel_api.panels)):
            await callback.answer("Invalid target", show_alert=True)
            await admin_restart_menu(callback)
            return
        await panel_api.restart_single(panel_idx)
        msg = "✅ Panel restarted."

    await callback.message.edit_text(msg, reply_markup=kb.admin_back_kb(), parse_mode="HTML")
