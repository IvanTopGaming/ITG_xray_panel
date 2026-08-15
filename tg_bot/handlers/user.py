import asyncio
import logging
import time
import datetime
from html import escape
from zoneinfo import ZoneInfo
from aiogram import Router, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
import httpx
from backend_client import BackendClient
from i18n import I18n
from runtime_config import runtime_config
from utils import generate_qr, format_bytes
from states import UserStates
import keyboards as kb

router = Router()
logger = logging.getLogger(__name__)


def h(value):
    return escape(str(value), quote=True)


async def safe_edit(message: types.Message, text: str, reply_markup=None, parse_mode="HTML"):

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
    return int(record.get("telegram_id") or 0) == int(user_id)


async def auto_expire_keys_message(
    message: types.Message,
    state: FSMContext,
    *,
    i18n: I18n,
    lang: str,
    client_id: str,
    delay: int = 60,
):

    await asyncio.sleep(delay)

    if await state.get_state() != UserStates.viewing_keys:
        return
    data = await state.get_data()
    if data.get("selected_key_client_id") != client_id:
        return

    try:
        text = await i18n.t("security.timeout", lang)
        back = await i18n.t("common.back_to_main", lang)
        show_again = await i18n.t("keys.actions.show_again", lang)
        await message.edit_text(
            text,
            reply_markup=kb.expired_keys_kb(
                show_again_label=show_again,
                back_label=back,
                client_id=client_id,
            ),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        logger.debug("auto_expire_keys_message: %s", exc)
    except Exception:
        logger.debug("auto_expire_keys_message: unexpected error", exc_info=True)


async def _render_welcome(
    message: types.Message,
    *,
    telegram_id: int,
    user_name: str = "",
    lang: str,
    i18n: I18n,
    backend: BackendClient,
    edit: bool = False,
) -> None:

    title = await i18n.t("welcome.title", lang, user_name=h(user_name))
    body = await i18n.t("welcome.body", lang)
    header = f"{title}\n\n{body}"

    user_state = None
    try:
        user_state = await backend.get_user_state(telegram_id)
    except Exception as exc:
        logger.info("get_user_state failed for %s: %s", telegram_id, exc)

    legacy_users = list((user_state or {}).get("clients") or [])
    has_clients = bool(legacy_users) or bool((user_state or {}).get("clients"))

    if has_clients:
        subs = await i18n.t("menu.subscription", lang)
        tariffs = await i18n.t("menu.tariffs", lang)
        stats = await i18n.t("menu.stats", lang)
        help_label = await i18n.t("menu.help", lang)
        markup = kb.user_main_kb(
            subs_label=subs,
            tariffs_label=tariffs,
            stats_label=stats,
            help_label=help_label,
        )
        if edit:
            await safe_edit(message, header, reply_markup=markup)
        else:
            await message.answer(header, reply_markup=markup, parse_mode="HTML")
        return

    if user_state is None:
        if edit:
            await safe_edit(message, header)
        else:
            await message.answer(header, parse_mode="HTML")
        return

    if user_state.get("trial_available"):
        trial = await i18n.t("trial.button.activate", lang, days=1)
        tariffs = await i18n.t("menu.tariffs", lang)
        help_label = await i18n.t("menu.help", lang)
        markup = kb.no_clients_menu_kb(
            trial_label=trial,
            tariffs_label=tariffs,
            help_label=help_label,
        )
        if edit:
            await safe_edit(message, header, reply_markup=markup)
        else:
            await message.answer(header, reply_markup=markup, parse_mode="HTML")
        return

    tariffs = await i18n.t("menu.tariffs", lang)
    help_label = await i18n.t("menu.help", lang)
    markup = kb.no_clients_menu_kb(
        trial_label=None,
        tariffs_label=tariffs,
        help_label=help_label,
    )
    if edit:
        await safe_edit(message, header, reply_markup=markup)
    else:
        await message.answer(header, reply_markup=markup, parse_mode="HTML")


async def _render_first_touch(
    message: types.Message,
    *,
    telegram_id: int,
    user_name: str,
    lang: str,
    i18n: I18n,
    backend: BackendClient,
    edit: bool = False,
) -> None:

    user_state = None
    try:
        user_state = await backend.get_user_state(telegram_id)
    except Exception as exc:
        logger.info("get_user_state failed for %s: %s", telegram_id, exc)

    has_clients = bool((user_state or {}).get("clients"))
    trial_available = bool((user_state or {}).get("trial_available"))

    if not user_state or has_clients or not trial_available:
        await _render_welcome(
            message,
            telegram_id=telegram_id,
            user_name=user_name,
            lang=lang,
            i18n=i18n,
            backend=backend,
            edit=edit,
        )
        return

    title = await i18n.t("onboarding.title", lang, user_name=h(user_name))
    activate = await i18n.t("trial.button.activate", lang, days=1)
    skip = await i18n.t("trial.button.skip", lang)
    markup = kb.first_touch_kb(activate_label=activate, skip_label=skip)
    if edit:
        await safe_edit(message, title, reply_markup=markup)
    else:
        await message.answer(title, reply_markup=markup, parse_mode="HTML")


@router.message(Command("start"))
async def cmd_start(
    message: types.Message,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    language_chosen: bool,
    backend: BackendClient,
):
    await state.clear()

    if not language_chosen:
        title = await i18n.t("lang_picker.title", "ru")
        en_label = await i18n.t("lang_picker.button.en", "ru")
        ru_label = await i18n.t("lang_picker.button.ru", "ru")
        await message.answer(
            title,
            reply_markup=kb.language_picker_kb(en_label, ru_label),
        )
        return

    user_name = message.from_user.first_name or message.from_user.username or ("друг" if lang == "ru" else "friend")
    await _render_welcome(
        message,
        telegram_id=message.from_user.id,
        user_name=user_name,
        lang=lang,
        i18n=i18n,
        backend=backend,
    )


@router.callback_query(F.data.startswith("set_lang:"))
async def cb_set_language(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    backend: BackendClient,
):
    new_lang = callback.data.split(":", 1)[1]
    if new_lang not in ("ru", "en"):
        await callback.answer("Unknown language", show_alert=True)
        return

    try:
        await backend.set_language(callback.from_user.id, new_lang)
    except Exception:
        logger.exception("set_language failed for %s", callback.from_user.id)
        await callback.answer("Не удалось сохранить выбор. Попробуйте ещё раз.", show_alert=True)
        return

    await callback.answer()
    await state.clear()
    user_name = (
        callback.from_user.first_name or callback.from_user.username or ("друг" if new_lang == "ru" else "friend")
    )
    await _render_first_touch(
        callback.message,
        telegram_id=callback.from_user.id,
        user_name=user_name,
        lang=new_lang,
        i18n=i18n,
        backend=backend,
        edit=True,
    )


@router.callback_query(F.data == "user_home")
async def user_home(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):
    await state.clear()
    user_name = callback.from_user.first_name or callback.from_user.username or ("друг" if lang == "ru" else "friend")
    await _render_welcome(
        callback.message,
        telegram_id=callback.from_user.id,
        user_name=user_name,
        lang=lang,
        i18n=i18n,
        backend=backend,
        edit=True,
    )


@router.callback_query(F.data == "user_help")
async def user_help(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
):
    await state.clear()
    body = await i18n.t("help.body", lang)
    back = await i18n.t("common.back_to_main", lang)
    await safe_edit(callback.message, body, reply_markup=kb.back_to_main_kb(back_label=back))


@router.callback_query(F.data == "user_sub")
async def user_sub(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):
    try:
        state_data = await backend.get_user_state(callback.from_user.id)
    except Exception as exc:
        logger.info("get_user_state failed: %s", exc)
        state_data = {}
    clients = list((state_data or {}).get("clients") or [])
    sub_url = (state_data or {}).get("sub_url")
    if not clients:
        await _render_no_subscription(callback, lang=lang, i18n=i18n, backend=backend)
        return

    await state.clear()
    open_ended_access = bool((state_data or {}).get("open_ended_access"))
    await state.update_data(open_ended_access=open_ended_access)
    renew_tariff_id = (
        None if open_ended_access else next((c.get("tariff_id") for c in clients if c.get("tariff_id")), None)
    )
    renew_label = await i18n.t("notification.button.renew", lang) if renew_tariff_id else None
    title = await i18n.t("sub.page.title", lang)
    open_label = await i18n.t("sub.actions.open_page", lang)
    keys_label = await i18n.t("sub.actions.show_keys", lang)
    help_label = await i18n.t("menu.help", lang)
    back_label = await i18n.t("common.back_to_main", lang)
    if sub_url:
        link_header = await i18n.t("sub.page.link_header", lang)
        copy_hint = await i18n.t("sub.page.copy_hint", lang)
        helper = await i18n.t("sub.page.url_helper", lang)
        body = f"{title}\n\n{link_header}\n\n<code>{h(sub_url)}</code>\n{copy_hint}\n\n{helper}"
    else:
        body = f"{title}\n\n" + await i18n.t("sub.page.no_url", lang)
    await safe_edit(
        callback.message,
        body,
        reply_markup=kb.user_sub_page_kb(
            open_label=open_label,
            keys_label=keys_label,
            help_label=help_label,
            back_label=back_label,
            sub_url=sub_url,
            qr_label=await i18n.t("sub.actions.show_qr", lang),
            renew_label=renew_label,
            renew_tariff_id=renew_tariff_id,
        ),
    )


async def _render_no_subscription(
    callback: types.CallbackQuery,
    *,
    lang: str,
    i18n: I18n,
    backend: BackendClient,
):

    msg = await i18n.t("home.no_subscription", lang)
    await callback.answer(msg, show_alert=True)

    try:
        user_name = (
            callback.from_user.first_name or callback.from_user.username or ("друг" if lang == "ru" else "friend")
        )
        await _render_welcome(
            callback.message,
            telegram_id=callback.from_user.id,
            user_name=user_name,
            lang=lang,
            i18n=i18n,
            backend=backend,
            edit=True,
        )
    except Exception as exc:
        logger.debug("re-render welcome skipped: %s", exc)


async def _render_keys_picker(
    callback: types.CallbackQuery,
    state: FSMContext,
    *,
    i18n: I18n,
    lang: str,
    clients: list,
):

    await state.clear()
    title = await i18n.t("keys.picker.title", lang)
    entry = await i18n.t("keys.list.entry", lang)
    back = await i18n.t("common.back_to_main", lang)
    await safe_edit(
        callback.message,
        title,
        reply_markup=kb.user_keys_list_kb(
            clients,
            entry_template=entry,
            back_label=back,
        ),
    )


@router.callback_query(F.data == "show_keys")
async def show_keys(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):
    try:
        state_data = await backend.get_user_state(callback.from_user.id)
        users_records = list((state_data or {}).get("clients") or [])
    except Exception as exc:
        logger.info("get_user_state failed: %s", exc)
        users_records = []
    if not users_records:
        await _render_no_subscription(callback, lang=lang, i18n=i18n, backend=backend)
        return

    if len(users_records) > 1:
        await _render_keys_picker(callback, state, i18n=i18n, lang=lang, clients=users_records)
        return

    await show_key_details(callback, state, users_records[0], i18n=i18n, lang=lang)


@router.callback_query(F.data.startswith("show_key_"))
async def user_key_selected(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):
    client_id = callback.data.removeprefix("show_key_")
    if not client_id:
        await callback.answer("Error")
        return

    try:
        state_data = await backend.get_user_state(callback.from_user.id)
    except Exception as exc:
        logger.info("get_user_state failed: %s", exc)
        await callback.answer("Service temporarily unavailable.", show_alert=True)
        return

    clients = (state_data or {}).get("clients", [])
    record = next(
        (c for c in clients if c.get("id") == client_id),
        None,
    )
    if not record:
        await callback.answer("Key not found")
        return
    if not is_record_owner(record, callback.from_user.id):
        await callback.answer("Access denied", show_alert=True)
        return

    await show_key_details(callback, state, record, i18n=i18n, lang=lang, has_other_keys=len(clients) > 1)


async def show_key_details(
    callback: types.CallbackQuery,
    state: FSMContext,
    record,
    *,
    i18n: I18n,
    lang: str,
    has_other_keys: bool = False,
):
    client_id = record["id"]

    await state.set_state(UserStates.viewing_keys)
    await state.update_data(selected_key_client_id=client_id)

    links = [link for link in (record.get("links") or []) if link]

    msg = callback.message

    if has_other_keys:
        back = await i18n.t("common.back_to_keys", lang)
        back_callback = "back_to_keys_picker"
    else:
        back = await i18n.t("common.back_to_main", lang)
        back_callback = "user_home"

    if not links:
        display = record.get("inbound_label") or record.get("inbound_tag") or record["email"]
        final_text = await i18n.t("keys.details.none", lang, email=h(display))
        msg = await safe_edit(
            msg,
            final_text,
            reply_markup=kb.sub_actions_kb(
                qr_label=await i18n.t("sub.actions.show_qr", lang),
                stats_label=await i18n.t("sub.actions.show_stats", lang),
                back_label=back,
                back_callback=back_callback,
            ),
        )
        return

    display = record.get("inbound_label") or record.get("inbound_tag") or record["email"]

    from urllib.parse import unquote

    await state.update_data(cached_links=links)

    if len(links) == 1:
        await _show_single_link(
            callback, state, links[0], record, i18n=i18n, lang=lang, back_label=back, back_callback=back_callback
        )
        return

    header = await i18n.t("keys.details.header", lang, email=h(display))
    self_destruct = await i18n.t("keys.details.self_destruct", lang)

    link_lines = []
    for link in links:
        label = unquote(link.rsplit("#", 1)[-1]) if "#" in link else ""
        if label:
            link_lines.append(f"🔑 <b>{h(label)}</b>\n<code>{h(link)}</code>")
        else:
            link_lines.append(f"<code>{h(link)}</code>")

    final_text = f"{header}\n\n" + "\n\n".join(link_lines) + f"\n\n{self_destruct}"

    cached = await state.get_data()
    renew_tariff_id = None if cached.get("open_ended_access") else record.get("tariff_id")
    renew_label = None
    if renew_tariff_id:
        renew_label = await i18n.t("notification.button.renew", lang)

    msg = await safe_edit(
        msg,
        final_text,
        reply_markup=kb.sub_actions_kb(
            qr_label=await i18n.t("sub.actions.show_qr", lang),
            stats_label=await i18n.t("sub.actions.show_stats", lang),
            back_label=back,
            back_callback=back_callback,
            renew_label=renew_label,
            renew_tariff_id=renew_tariff_id,
        ),
    )
    asyncio.create_task(auto_expire_keys_message(msg, state, i18n=i18n, lang=lang, client_id=client_id))


async def _show_single_link(callback, state, link, record, *, i18n, lang, back_label, back_callback):

    client_id = record["id"]
    display = record.get("inbound_label") or record.get("inbound_tag") or record["email"]
    header = await i18n.t("keys.details.header", lang, email=h(display))
    self_destruct = await i18n.t("keys.details.self_destruct", lang)
    final_text = f"{header}\n\n<code>{h(link)}</code>\n\n{self_destruct}"

    cached = await state.get_data()
    renew_tariff_id = None if cached.get("open_ended_access") else record.get("tariff_id")
    renew_label = None
    if renew_tariff_id:
        renew_label = await i18n.t("notification.button.renew", lang)

    msg = await safe_edit(
        callback.message,
        final_text,
        reply_markup=kb.sub_actions_kb(
            qr_label=await i18n.t("sub.actions.show_qr", lang),
            stats_label=await i18n.t("sub.actions.show_stats", lang),
            back_label=back_label,
            back_callback=back_callback,
            renew_label=renew_label,
            renew_tariff_id=renew_tariff_id,
        ),
    )
    asyncio.create_task(auto_expire_keys_message(msg, state, i18n=i18n, lang=lang, client_id=client_id))


@router.callback_query(F.data.startswith("show_link_"))
async def show_selected_link(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
):
    idx_str = callback.data.removeprefix("show_link_")
    try:
        idx = int(idx_str)
    except ValueError:
        await callback.answer("Invalid link", show_alert=True)
        return

    data = await state.get_data()
    links = data.get("cached_links", [])
    if idx < 0 or idx >= len(links):
        await callback.answer("Link not found", show_alert=True)
        return

    link = links[idx]
    label = link.rsplit("#", 1)[-1] if "#" in link else f"Key {idx + 1}"
    from urllib.parse import unquote

    label = unquote(label)

    self_destruct = await i18n.t("keys.details.self_destruct", lang)
    text = f"🔑 <b>{h(label)}</b>\n\n<code>{h(link)}</code>\n\n{self_destruct}"

    client_id = data.get("selected_key_client_id")
    msg = await safe_edit(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_link_picker")],
            ]
        ),
    )
    asyncio.create_task(auto_expire_keys_message(msg, state, i18n=i18n, lang=lang, client_id=client_id))


@router.callback_query(F.data == "back_to_link_picker")
async def back_to_link_picker(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
):
    data = await state.get_data()
    links = data.get("cached_links", [])
    back = await i18n.t("common.back_to_main", lang)
    header = "🔑 Select a key:"
    await safe_edit(
        callback.message,
        header,
        reply_markup=kb.key_picker_kb(links, back_label=back, back_callback="user_home"),
    )


@router.callback_query(F.data == "back_to_keys")
async def back_to_keys(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):

    data = await state.get_data()
    client_id = data.get("selected_key_client_id")

    if not client_id:
        await user_home(callback, state, i18n=i18n, lang=lang, backend=backend)
        return

    try:
        state_data = await backend.get_user_state(callback.from_user.id)
    except Exception as exc:
        logger.info("get_user_state failed: %s", exc)
        await user_home(callback, state, i18n=i18n, lang=lang, backend=backend)
        return

    clients = (state_data or {}).get("clients", [])
    record = next(
        (c for c in clients if c.get("id") == client_id),
        None,
    )
    if not record or not is_record_owner(record, callback.from_user.id):
        await user_home(callback, state, i18n=i18n, lang=lang, backend=backend)
        return

    await show_key_details(callback, state, record, i18n=i18n, lang=lang, has_other_keys=len(clients) > 1)


@router.callback_query(F.data == "back_to_keys_picker")
async def back_to_keys_picker(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):

    try:
        state_data = await backend.get_user_state(callback.from_user.id)
        users_records = list((state_data or {}).get("clients") or [])
    except Exception as exc:
        logger.info("get_user_state failed: %s", exc)
        await user_home(callback, state, i18n=i18n, lang=lang, backend=backend)
        return

    if len(users_records) <= 1:
        if users_records:
            await show_key_details(callback, state, users_records[0], i18n=i18n, lang=lang)
        else:
            await user_home(callback, state, i18n=i18n, lang=lang, backend=backend)
        return

    await _render_keys_picker(callback, state, i18n=i18n, lang=lang, clients=users_records)


@router.callback_query(F.data == "show_qr")
async def qr_for_key(
    callback: types.CallbackQuery,
    state: FSMContext,
    backend: BackendClient,
    i18n: I18n,
    lang: str,
):
    data = await state.get_data()
    client_id = data.get("selected_key_client_id")

    if not client_id:
        await callback.answer("Session expired, please select key again.")
        await user_sub(callback, state, i18n, lang, backend)
        return

    try:
        state_data = await backend.get_user_state(callback.from_user.id)
    except Exception as exc:
        logger.info("get_user_state failed: %s", exc)
        await callback.answer("Service temporarily unavailable.", show_alert=True)
        return

    record = next(
        (c for c in (state_data or {}).get("clients", []) if c.get("id") == client_id),
        None,
    )
    if not record:
        await callback.answer("Key not found")
        return
    if not is_record_owner(record, callback.from_user.id):
        await callback.answer("Access denied", show_alert=True)
        return

    links = [link for link in (record.get("links") or []) if link]
    if not links:
        await callback.answer(await i18n.t("qr.no_link", lang), show_alert=True)
        return

    display = record.get("inbound_label") or record.get("inbound_tag") or record.get("email", "")
    await _send_qr(callback, links[0], caption=f"\U0001f4f1 <b>{h(display)}</b>", i18n=i18n, lang=lang)


@router.callback_query(F.data == "sub_qr")
async def qr_for_subscription(
    callback: types.CallbackQuery,
    state: FSMContext,
    backend: BackendClient,
    i18n: I18n,
    lang: str,
):
    try:
        state_data = await backend.get_user_state(callback.from_user.id)
    except Exception as exc:
        logger.info("get_user_state failed: %s", exc)
        await callback.answer("Service temporarily unavailable.", show_alert=True)
        return

    sub_url = (state_data or {}).get("sub_url")
    if not sub_url:
        await callback.answer(await i18n.t("qr.no_link", lang), show_alert=True)
        return

    await _send_qr(
        callback,
        sub_url,
        caption=await i18n.t("sub.page.title", lang),
        i18n=i18n,
        lang=lang,
        back_callback="user_sub",
        back_key="common.back_to_main",
    )


async def _send_qr(
    callback,
    data,
    *,
    caption,
    i18n: I18n,
    lang: str,
    back_callback: str = "back_to_keys",
    back_key: str = "common.back_to_keys",
):

    qr_file = generate_qr(data)
    back = await i18n.t(back_key, lang)

    await callback.message.delete()
    await callback.message.answer_photo(
        qr_file,
        caption=caption,
        reply_markup=kb.qr_back_kb(back_label=back, back_callback=back_callback),
        parse_mode="HTML",
    )


async def _format_expiry(expiry_ts_ms, *, i18n: I18n, lang: str) -> str:

    if expiry_ts_ms <= 0:
        return await i18n.t("stats.expiry.permanent", lang)

    now_ms = int(time.time() * 1000)
    diff_ms = expiry_ts_ms - now_ms
    try:
        tz = ZoneInfo(runtime_config.display_timezone or "Europe/Moscow")
    except Exception:
        tz = ZoneInfo("UTC")
    expiry_dt = datetime.datetime.fromtimestamp(expiry_ts_ms / 1000, tz=tz)
    date_str = expiry_dt.strftime("%Y-%m-%d")

    if diff_ms <= 0:
        return await i18n.t("stats.expiry.expired", lang, date=date_str)

    days = diff_ms / (1000 * 60 * 60 * 24)
    if days < 1:
        hours = int(diff_ms / (1000 * 60 * 60))
        return await i18n.t("stats.expiry.hours_left", lang, date=date_str, hours=hours)

    if days <= 3:
        indicator = "🔴"
    elif days <= 7:
        indicator = "🟡"
    else:
        indicator = "🟢"
    return await i18n.t(
        "stats.expiry.days_left",
        lang,
        indicator=indicator,
        date=date_str,
        days=int(days),
    )


def _progress_bar(percent, length=12):
    filled = min(length, int(length * percent / 100))
    return "█" * filled + "░" * (length - filled)


def _record_total(record) -> int:
    return int(record.get("up") or 0) + int(record.get("down") or 0)


async def _format_key_stats(record, *, i18n: I18n, lang: str) -> str:

    enable = bool(record.get("enable", True))
    key_total = _record_total(record)
    limit = int(record.get("limit_bytes") or 0)
    expiry = int(record.get("expiry_time") or 0)
    server = record.get("panel_name")

    status_key = "stats.key.status_active" if enable else "stats.key.status_disabled"
    status = await i18n.t(status_key, lang)
    display = record.get("inbound_label") or record.get("inbound_tag") or record.get("email", "")
    if server:
        display = f"{display} · {server}"

    lines = [f"🔑 <b>{h(display)}</b>  {status}"]

    if limit > 0:
        percent = min(100.0, (key_total / limit) * 100)
        remaining = max(0, limit - key_total)
        bar = _progress_bar(percent)
        used_label = await i18n.t("stats.key.used_label", lang)
        left_label = await i18n.t("stats.key.left_label", lang)
        lines.append(
            f"  📦 <code>{bar}</code> {percent:.1f}%\n"
            f"  {used_label}  <b>{format_bytes(key_total)}</b> / {format_bytes(limit)}\n"
            f"  {left_label}  <b>{format_bytes(remaining)}</b>"
        )
    else:
        lines.append(await i18n.t("stats.key.unlimited", lang, used=format_bytes(key_total)))

    expiry_str = await _format_expiry(expiry, i18n=i18n, lang=lang)
    lines.append(await i18n.t("stats.key.expiry_label", lang, expiry=expiry_str))

    return "\n".join(lines)


@router.callback_query(F.data == "user_stats")
async def user_stats(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):
    await state.clear()
    try:
        state_data = await backend.get_user_state(callback.from_user.id)
        users_records = list((state_data or {}).get("clients") or [])
    except Exception as exc:
        logger.info("get_user_state failed: %s", exc)
        users_records = []
    if not users_records:
        return

    msg = callback.message

    key_blocks = []
    grand_total = 0

    for record in users_records:
        grand_total += _record_total(record)
        key_blocks.append(await _format_key_stats(record, i18n=i18n, lang=lang))

    header_line = await i18n.t("stats.header", lang)
    user_line = await i18n.t(
        "stats.user_line",
        lang,
        user_name=h(callback.from_user.first_name or ""),
    )
    header = f"{header_line}\n{user_line}"
    if len(users_records) > 1:
        header += "\n" + await i18n.t(
            "stats.grand_total",
            lang,
            total=format_bytes(grand_total),
        )

    final_text = header + "\n\n" + "\n\n".join(key_blocks)

    back = await i18n.t("common.back_to_main", lang)
    refresh = await i18n.t("stats.actions.refresh", lang)
    await safe_edit(
        msg,
        final_text,
        reply_markup=kb.user_stats_kb(refresh_label=refresh, back_label=back),
    )
    await callback.answer()


@router.callback_query(F.data == "trial:skip")
async def cb_trial_skip(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):

    await callback.answer()
    await state.clear()
    user_name = callback.from_user.first_name or callback.from_user.username or ("друг" if lang == "ru" else "friend")
    await _render_welcome(
        callback.message,
        telegram_id=callback.from_user.id,
        user_name=user_name,
        lang=lang,
        i18n=i18n,
        backend=backend,
        edit=True,
    )


@router.callback_query(F.data == "trial:activate")
async def cb_trial_activate(
    callback: types.CallbackQuery,
    i18n: I18n,
    lang: str,
    backend: BackendClient,
):
    try:
        result = await backend.activate_trial(callback.from_user.id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            already = await i18n.t("trial.already_used", lang)
            try:
                await callback.message.edit_text(already)
            except Exception:
                await callback.message.answer(already)
        else:
            logger.warning("activate_trial returned %s", exc.response.status_code)
            await callback.answer("Error, try again later", show_alert=True)
        await callback.answer()
        return
    except Exception:
        logger.exception("activate_trial unexpected error")
        await callback.answer("Error, try again later", show_alert=True)
        return

    expires_ms = int(result.get("expires_at_ms") or 0)
    if expires_ms <= 0:
        expires_str = await i18n.t("stats.expiry.permanent", lang)
    else:
        try:
            _tz = ZoneInfo(runtime_config.display_timezone or "Europe/Moscow")
        except Exception:
            _tz = ZoneInfo("UTC")
        expires_str = datetime.datetime.fromtimestamp(expires_ms / 1000, tz=_tz).strftime("%d.%m.%Y %H:%M")
    success = await i18n.t("trial.success", lang, expires_at=expires_str)
    subs_label = await i18n.t("menu.subscription", lang)
    back_label = await i18n.t("common.back_to_main", lang)
    markup = kb.trial_success_kb(subs_label=subs_label, back_label=back_label)
    try:
        await callback.message.edit_text(success, reply_markup=markup)
    except Exception:
        await callback.message.answer(success, reply_markup=markup)
    await callback.answer()
