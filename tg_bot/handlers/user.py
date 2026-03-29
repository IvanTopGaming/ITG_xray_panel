import asyncio
import logging
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
        if message.content_type == types.ContentType.PHOTO:
            await message.edit_caption(
                caption="🔒 <b>Security Timeout</b>\n\nQR Code hidden.",
                reply_markup=kb.back_to_main_kb(),
                parse_mode="HTML",
            )
        else:
            await message.edit_text(
                "🔒 <b>Security Timeout</b>\n\nData hidden for security.",
                reply_markup=kb.back_to_main_kb(),
                parse_mode="HTML",
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

    msg_kwargs = {
        "text": "🛡️ Main Menu",
        "reply_markup": kb.user_main_kb(),
        "parse_mode": "HTML",
    }

    if callback.message.content_type == types.ContentType.PHOTO:
        await callback.message.delete()
        await callback.message.answer(**msg_kwargs)
    else:
        try:
            await callback.message.edit_text(**msg_kwargs)
        except TelegramBadRequest:
            await callback.message.answer(**msg_kwargs)


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
    await callback.message.edit_text(text, reply_markup=kb.back_to_main_kb(), parse_mode="HTML")


@router.callback_query(F.data == "user_sub")
async def user_sub(callback: types.CallbackQuery, state: FSMContext):
    users_records = db.get_users_by_tg_id(callback.from_user.id)
    if not users_records:
        await callback.answer("User not found", show_alert=True)
        return

    if len(users_records) > 1:
        await state.clear()

        if callback.message.content_type == types.ContentType.PHOTO:
            await callback.message.delete()
            await callback.message.answer(
                "🔑 <b>Select Key</b>\nYou have multiple keys available:",
                reply_markup=kb.user_keys_list_kb(users_records),
                parse_mode="HTML",
            )
        else:
            await callback.message.edit_text(
                "🔑 <b>Select Key</b>\nYou have multiple keys available:",
                reply_markup=kb.user_keys_list_kb(users_records),
                parse_mode="HTML",
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

    loading_text = f"🔍 <b>Fetching keys for {h(email)}...</b>\n<i>Connecting to servers...</i>"

    msg = None
    if callback.message.content_type == types.ContentType.PHOTO:
        await callback.message.delete()
        msg = await callback.message.answer(loading_text, parse_mode="HTML")
    else:
        try:
            await callback.message.edit_text(loading_text, parse_mode="HTML")
            msg = callback.message
        except TelegramBadRequest:
            msg = await callback.message.answer(loading_text, parse_mode="HTML")

    tasks = []
    for idx in range(len(panel_api.panels)):
        tasks.append(panel_api.get_subscription_link_single(email, idx, inbound_tag=inbound_tag))

    links = []

    for coro in asyncio.as_completed(tasks):
        try:
            res = await coro
        except Exception as exc:
            logger.warning("Failed to fetch subscription links: %s", exc)
            continue
        if res:
            links.extend(res)

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

    try:
        await msg.edit_text(final_text, reply_markup=kb.sub_actions_kb(), parse_mode="HTML")
    except TelegramBadRequest:
        pass

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

    await callback.message.edit_text(
        "🖥 <b>Select Server</b>\nChoose which key to display as QR:",
        reply_markup=kb.user_qr_server_kb(panel_api.panels),
        parse_mode="HTML",
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


@router.callback_query(F.data == "user_stats")
async def user_stats(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    users_records = db.get_users_by_tg_id(callback.from_user.id)
    if not users_records:
        return

    msg = None
    if callback.message.content_type == types.ContentType.PHOTO:
        await callback.message.delete()
        msg = await callback.message.answer("📊 <b>Fetching Statistics...</b>", parse_mode="HTML")
    else:
        await callback.message.edit_text(
            "📊 <b>Fetching Statistics...</b>\n<i>Aggregating all keys...</i>",
            parse_mode="HTML",
        )
        msg = callback.message

    total_up = 0
    total_down = 0

    report_text = ""

    for record in users_records:
        email = record[3]
        inbound_tag = record[4]
        try:
            stats = await panel_api.get_client_stats_aggregate(email, inbound_tag=inbound_tag)
        except Exception as exc:
            logger.warning("Failed to fetch stats for %s: %s", email, exc)
            continue

        if stats:
            key_up = stats["up"]
            key_down = stats["down"]
            key_total = stats["total"]
            limit = stats["limit"]
            enable = stats["enable"]

            total_up += key_up
            total_down += key_down

            status_icon = "🟢" if enable else "🔴"
            limit_str = format_bytes(limit) if limit > 0 else "∞"

            report_text += (
                f"🔑 <b>{h(email)}</b> ({h(inbound_tag)}) {status_icon}\n"
                f"   📦 {format_bytes(key_total)} / {limit_str}\n"
            )

    grand_total = total_up + total_down

    final_text = (
        f"<b>📊 Global Usage Statistics</b>\n\n"
        f"👤 <b>Account:</b> {h(callback.from_user.first_name)}\n"
        f"📦 <b>Total Consumed:</b> <code>{format_bytes(grand_total)}</code>\n"
        f"-------------------\n"
        f"{report_text}"
    )

    try:
        await msg.edit_text(final_text, reply_markup=kb.back_to_main_kb(), parse_mode="HTML")
    except TelegramBadRequest:
        pass
