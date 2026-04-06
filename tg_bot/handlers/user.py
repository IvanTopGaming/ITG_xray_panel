import asyncio
import logging
import time
import datetime
from html import escape
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from database import db
from api_service import panel_api
from utils import generate_qr, format_bytes
from states import UserStates
import keyboards as kb

router = Router()
logger = logging.getLogger(__name__)


def h(value):
    return escape(str(value), quote=True)


async def safe_edit(message: types.Message, text: str, reply_markup=None, parse_mode="HTML"):
    """Edit or replace a message regardless of its current content type.

    - Text message → edit in-place (no flicker).
    - Photo message → delete + send new text (photo must be removed).
    - Swallows "message is not modified" silently to avoid duplicate messages
      when the user taps the same button twice.
    - Falls back to delete+send only when the message truly can't be edited.
    """
    kwargs = {"reply_markup": reply_markup, "parse_mode": parse_mode}
    if message.content_type == types.ContentType.PHOTO:
        try:
            await message.delete()
        except Exception:
            pass
        return await message.answer(text, **kwargs)
    try:
        await message.edit_text(text, **kwargs)
        return message
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return message
        try:
            await message.delete()
        except Exception:
            pass
        return await message.answer(text, **kwargs)


def is_record_owner(record, user_id):
    if not record:
        return False
    return int(record[1]) == int(user_id)


async def auto_expire_message(message: types.Message, state: FSMContext, expected_state: str, delay: int = 60):
    await asyncio.sleep(delay)

    current_state = await state.get_state()
    if current_state != expected_state:
        return

    try:
        await safe_edit(
            message,
            "🔒 <b>Security Timeout</b>\n\nData hidden for security.",
            reply_markup=kb.back_to_main_kb(),
        )
    except Exception:
        logger.debug("Failed to auto-expire sensitive message", exc_info=True)


@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    users = db.get_users_by_tg_id(message.from_user.id)

    if users:
        await message.answer(
            f"👋 Welcome back, <b>{h(message.from_user.first_name)}</b>!",
            reply_markup=kb.user_main_kb(),
            parse_mode="HTML",
        )
    else:
        await message.answer("⛔️ <b>Access Denied</b>\nContact admin.", parse_mode="HTML")


@router.callback_query(F.data == "user_home")
async def user_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message, "🛡️ Main Menu", reply_markup=kb.user_main_kb())


@router.callback_query(F.data == "user_help")
async def user_help(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "<b>📚 Setup Guide</b>\n\n"
        "1. Install <b>Happ</b> (Android) or <b>V2Box</b> (iOS).\n"
        "2. Click 'My Subscription'.\n"
        "3. Copy all keys.\n"
        "4. Import from Clipboard in the app."
    )
    await safe_edit(callback.message, text, reply_markup=kb.back_to_main_kb())


@router.callback_query(F.data == "user_sub")
async def user_sub(callback: types.CallbackQuery, state: FSMContext):
    users_records = db.get_users_by_tg_id(callback.from_user.id)
    if not users_records:
        await callback.answer("User not found", show_alert=True)
        return

    if len(users_records) > 1:
        await state.clear()
        await safe_edit(
            callback.message,
            "🔑 <b>Select Key</b>\nYou have multiple keys available:",
            reply_markup=kb.user_keys_list_kb(users_records),
        )
        return

    await show_key_details(callback, state, users_records[0])


@router.callback_query(F.data.startswith("show_key_"))
async def user_key_selected(callback: types.CallbackQuery, state: FSMContext):
    try:
        db_id = int(callback.data.split("_")[2])
    except (ValueError, IndexError):
        await callback.answer("Error")
        return

    record = db.get_user_by_db_id(db_id)
    if not record:
        await callback.answer("Key not found")
        return
    if not is_record_owner(record, callback.from_user.id):
        await callback.answer("Access denied", show_alert=True)
        return

    await show_key_details(callback, state, record)


async def show_key_details(callback: types.CallbackQuery, state: FSMContext, record):
    db_id = record[0]
    email = record[3]
    inbound_tag = record[4]

    await state.set_state(UserStates.viewing_keys)
    await state.update_data(selected_key_db_id=db_id)

    tasks = [
        panel_api.get_subscription_link_single(email, idx, inbound_tag=inbound_tag)
        for idx in range(len(panel_api.panels))
    ]

    links = []
    for coro in asyncio.as_completed(tasks):
        try:
            res = await coro
        except Exception as exc:
            logger.warning("Failed to fetch subscription links: %s", exc)
            continue
        if res:
            links.extend(res)

    msg = callback.message

    final_text = ""
    if links:
        links_text = "\n\n".join([f"<code>{h(link)}</code>" for link in links])
        final_text = (
            f"<b>🔑 Access Keys: {h(email)}</b>\n"
            "<i>Click to copy:</i>\n\n"
            f"{links_text}\n\n"
            "⚠️ <i>Self-destructs in 60s.</i>"
        )
    else:
        final_text = f"❌ <b>No active keys found for {h(email)}.</b>\nContact admin."

    msg = await safe_edit(msg, final_text, reply_markup=kb.sub_actions_kb())

    if links:
        asyncio.create_task(auto_expire_message(msg, state, UserStates.viewing_keys))


@router.callback_query(F.data == "back_to_keys")
async def back_to_keys(callback: types.CallbackQuery, state: FSMContext):
    """Go back to the keys view by re-fetching — used from QR screen."""
    data = await state.get_data()
    db_id = data.get("selected_key_db_id")

    if not db_id:
        await user_home(callback, state)
        return

    record = db.get_user_by_db_id(db_id)
    if not record or not is_record_owner(record, callback.from_user.id):
        await user_home(callback, state)
        return

    await show_key_details(callback, state, record)


@router.callback_query(F.data == "qr_select_server")
async def qr_select_server(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.viewing_qr)
    await safe_edit(
        callback.message,
        "🖥 <b>Select Server</b>\nChoose which key to display as QR:",
        reply_markup=kb.user_qr_server_kb(panel_api.panels),
    )


@router.callback_query(UserStates.viewing_qr, F.data.startswith("qr_gen_"))
async def qr_generate_for_server(callback: types.CallbackQuery, state: FSMContext):
    try:
        idx = int(callback.data.split("_")[2])
    except (ValueError, IndexError):
        await callback.answer("Invalid selection")
        return

    data = await state.get_data()
    db_id = data.get("selected_key_db_id")

    if not db_id:
        await callback.answer("Session expired, please select key again.")
        await user_sub(callback, state)
        return

    record = db.get_user_by_db_id(db_id)
    if not record:
        await callback.answer("Key not found")
        return
    if not is_record_owner(record, callback.from_user.id):
        await callback.answer("Access denied", show_alert=True)
        return

    email = record[3]
    inbound_tag = record[4]
    raw_links = await panel_api.get_subscription_link_single(email, idx, inbound_tag=inbound_tag)
    link = raw_links[0] if raw_links else None

    if link:
        if "#" in link:
            link = link.split("#")[0] + f"#{panel_api.panels[idx].name}"

        qr_file = generate_qr(link)

        await callback.message.delete()
        msg = await callback.message.answer_photo(
            qr_file,
            caption=f"📱 <b>{h(panel_api.panels[idx].name)} QR</b>\n\n⚠️ <i>Valid for 60 seconds.</i>",
            reply_markup=kb.qr_back_kb(),
            parse_mode="HTML",
        )
        asyncio.create_task(auto_expire_message(msg, state, UserStates.viewing_qr))
    else:
        await callback.answer("❌ No active key found on this server.", show_alert=True)


def _format_expiry(expiry_ts_ms):
    """Return (display_str, days_left_float). days_left is None if permanent."""
    if expiry_ts_ms <= 0:
        return "♾️ Permanent", None
    now_ms = int(time.time() * 1000)
    diff_ms = expiry_ts_ms - now_ms
    if diff_ms <= 0:
        expiry_dt = datetime.datetime.fromtimestamp(expiry_ts_ms / 1000)
        return f"❌ Expired ({expiry_dt.strftime('%Y-%m-%d')})", 0.0
    days = diff_ms / (1000 * 60 * 60 * 24)
    expiry_dt = datetime.datetime.fromtimestamp(expiry_ts_ms / 1000)
    date_str = expiry_dt.strftime("%Y-%m-%d")
    if days < 1:
        hours = int(diff_ms / (1000 * 60 * 60))
        return f"⚠️ {date_str} ({hours}h left)", days
    elif days <= 3:
        return f"🔴 {date_str} ({int(days)}d left)", days
    elif days <= 7:
        return f"🟡 {date_str} ({int(days)}d left)", days
    else:
        return f"🟢 {date_str} ({int(days)}d left)", days


def _progress_bar(percent, length=12):
    filled = min(length, int(length * percent / 100))
    return "█" * filled + "░" * (length - filled)


def _format_key_stats(email, inbound_tag, stats):
    enable = stats["enable"]
    key_total = stats["total"]
    limit = stats["limit"]
    expiry = stats["expiry"]
    per_server = stats.get("per_server", [])

    status = "🟢 Active" if enable else "🔴 Disabled"
    tag_display = f" <code>[{h(inbound_tag)}]</code>" if inbound_tag and inbound_tag.lower() != "multi" else ""

    lines = [f"🔑 <b>{h(email)}</b>{tag_display}  {status}"]

    # Traffic block
    if limit > 0:
        percent = min(100.0, (key_total / limit) * 100)
        remaining = max(0, limit - key_total)
        bar = _progress_bar(percent)
        lines.append(
            f"  📦 <code>{bar}</code> {percent:.1f}%\n"
            f"  Used:  <b>{format_bytes(key_total)}</b> / {format_bytes(limit)}\n"
            f"  Left:  <b>{format_bytes(remaining)}</b>"
        )
    else:
        lines.append(f"  📦 Used: <b>{format_bytes(key_total)}</b> / ∞")

    # Expiry block
    expiry_str, _ = _format_expiry(expiry)
    lines.append(f"  📅 Expiry: {expiry_str}")

    # Per-server breakdown (only if more than one server)
    if len(per_server) > 1:
        server_lines = []
        for s in per_server:
            server_lines.append(f"    • {h(s['name'])}: ↑{format_bytes(s['up'])} ↓{format_bytes(s['down'])}")
        lines.append("  🖥 Per server:\n" + "\n".join(server_lines))

    return "\n".join(lines)


@router.callback_query(F.data == "user_stats")
async def user_stats(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    users_records = db.get_users_by_tg_id(callback.from_user.id)
    if not users_records:
        return

    tasks = [panel_api.get_client_stats_aggregate(record[3], inbound_tag=record[4]) for record in users_records]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    msg = callback.message

    key_blocks = []
    grand_total = 0

    for record, result in zip(users_records, results):
        email = record[3]
        inbound_tag = record[4]
        if isinstance(result, Exception) or result is None:
            key_blocks.append(f"🔑 <b>{h(email)}</b> — ❌ unavailable")
            continue
        grand_total += result["total"]
        key_blocks.append(_format_key_stats(email, inbound_tag, result))

    header = f"<b>📊 Statistics</b>\n👤 {h(callback.from_user.first_name)}"
    if len(users_records) > 1:
        header += f"\n📦 Total: <b>{format_bytes(grand_total)}</b>"

    final_text = header + "\n\n" + "\n\n".join(key_blocks)

    await safe_edit(msg, final_text, reply_markup=kb.back_to_main_kb())
