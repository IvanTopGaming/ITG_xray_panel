import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend_client import BackendClient
from handlers import catalog
from i18n import I18n
from keyboards import user_keys_list_kb
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from handlers import user
from states import UserStates


def callback_for(data):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=7654321),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )


async def test_pending_trial_does_not_claim_access_is_issued():
    callback = callback_for("trial:activate")
    backend = SimpleNamespace(
        activate_trial=AsyncMock(return_value={"status": "pending", "panel_failures": [{"panel_id": 1}]})
    )
    i18n = SimpleNamespace(t=AsyncMock(side_effect=lambda key, *args, **kw: key))
    await user.cb_trial_activate(callback, i18n, "en", backend)
    callback.answer.assert_awaited_once_with("trial.pending", show_alert=True)
    callback.message.edit_text.assert_not_awaited()
    assert all(call.args[0] != "trial.success" for call in i18n.t.await_args_list)


@pytest.mark.parametrize("failure", ["ack", "placeholder"])
async def test_checkout_setup_failure_releases_user(failure):
    callback = callback_for("buy:1")
    failing = callback.answer if failure == "ack" else callback.message.edit_text
    failing.side_effect = RuntimeError("temporary failure")
    i18n = SimpleNamespace(t=AsyncMock(return_value="creating"))
    try:
        with pytest.raises(RuntimeError):
            await catalog.start_checkout(callback, None, i18n, "en", None)
        assert callback.from_user.id not in catalog._checkout_in_flight
    finally:
        catalog._checkout_in_flight.discard(callback.from_user.id)


@pytest.mark.parametrize("result", [None, {"status": "pending"}, {"status": "processing"}])
async def test_unknown_cancellation_preserves_invoice(result):
    callback = callback_for("cancel:1")
    backend = SimpleNamespace(cancel_payment=AsyncMock(return_value=result))
    i18n = SimpleNamespace(t=AsyncMock(side_effect=lambda key, *args, **kw: key))
    await catalog.cancel_payment(callback, None, i18n, "en", backend)
    assert callback.message.edit_text.await_count == 0


async def test_failed_cancellation_preserves_invoice():
    callback = callback_for("cancel:1")
    backend = SimpleNamespace(cancel_payment=AsyncMock(side_effect=RuntimeError("unreachable")))
    i18n = SimpleNamespace(t=AsyncMock(side_effect=lambda key, *args, **kw: key))
    await catalog.cancel_payment(callback, None, i18n, "en", backend)
    assert callback.message.edit_text.await_count == 0


async def test_invalid_saved_template_uses_safe_fallback():
    i18n = I18n(SimpleNamespace(get_texts=AsyncMock(return_value={"texts": {"welcome.title": "Hi {user_name"}})))
    result = await i18n.t("welcome.title", "en", user_name="Alice")
    assert result and "{" not in result and "⟨" not in result


async def test_stale_text_cache_limits_failed_refreshes():
    backend = SimpleNamespace(get_texts=AsyncMock(side_effect=RuntimeError("unreachable")))
    i18n = I18n(backend)
    i18n._cache["en"] = {"one": "One", "two": "Two"}
    i18n._loaded_at["en"] = time.time() - 120
    assert await i18n.t("one", "en") == "One"
    assert await i18n.t("two", "en") == "Two"
    assert backend.get_texts.await_count == 1


def test_invalid_keyboard_template_keeps_key_accessible():
    keyboard = user_keys_list_kb([{"id": "12", "inbound_label": "Node"}], entry_template="{name", back_label="Back")
    assert keyboard.inline_keyboard[0][0].text == "🔑 Node"
    assert keyboard.inline_keyboard[0][0].callback_data == "show_key_12"


async def test_checkout_alone_covers_provider_retry_budget():
    budgets = {}

    async def receive(request):
        budgets[request.url.path] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json={})

    backend = BackendClient("http://backend", "test")
    backend._client = httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(receive), timeout=10)
    try:
        await backend.create_checkout(1, 2, "en")
        await backend.get_user_state(1)
    finally:
        await backend.close()
    assert budgets["/billing/checkout"] >= 20
    assert budgets["/bot-service/users/1/state"] == 10


async def test_runtime_swaps_keep_main_and_consumer_alive(monkeypatch):
    monkeypatch.setenv("BACKEND_API_URL", "http://backend")
    import main

    starts = asyncio.Queue()
    consumer_stopped = asyncio.Event()
    bots = []

    class Dispatcher:
        def __init__(self, **kwargs):
            self.message = self.callback_query = SimpleNamespace(middleware=lambda value: None)
            self.stopped = None
            self.active = 0

        def include_router(self, router):
            pass

        async def start_polling(self, bot, **kwargs):
            self.active += 1
            assert self.active == 1
            self.stopped = asyncio.Event()
            await starts.put(bot)
            try:
                await self.stopped.wait()
            finally:
                self.active -= 1

        async def stop_polling(self):
            self.stopped.set()
            await asyncio.sleep(0)

    def build():
        bot = SimpleNamespace(
            session=SimpleNamespace(close=AsyncMock()),
            delete_webhook=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(username="test")),
        )
        bots.append(bot)
        return bot

    runtime = SimpleNamespace(
        bootstrap=AsyncMock(),
        close=AsyncMock(),
        set_bot_username=lambda value: None,
        set_change_listener=lambda listener: setattr(runtime, "listener", listener),
        refresh_loop=lambda: asyncio.Event().wait(),
    )

    async def consume(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            consumer_stopped.set()

    monkeypatch.setattr(main, "runtime_config", runtime)
    monkeypatch.setattr(main, "Dispatcher", Dispatcher)
    monkeypatch.setattr(main, "_build_bot", build)
    monkeypatch.setattr(main, "run_consumer", consume)
    monkeypatch.setattr(main.config, "BOT_SERVICE_TOKEN", "test")
    task = asyncio.create_task(main.main())
    try:
        await asyncio.wait_for(starts.get(), 1)
        for _ in range(2):
            await runtime.listener(True)
            await asyncio.wait_for(starts.get(), 1)
            assert not task.done()
            assert not consumer_stopped.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(bots) == 3
    assert all(bot.session.close.await_count == 1 for bot in bots)


async def test_consumer_bad_payload_does_not_reconnect_and_closes_resources(monkeypatch):
    import bot_events_consumer as consumer

    waiting = asyncio.Event()
    messages = [
        {"type": "message", "data": "[]"},
        {"type": "message", "data": '{"type":"traffic_notification","telegram_id":1,"payload":{"used_bytes":"bad"}}'},
        {"type": "message", "data": '{"type":"texts_changed","payload":{"lang":"en"}}'},
    ]

    async def receive(**kwargs):
        if messages:
            return messages.pop(0)
        waiting.set()
        await asyncio.Event().wait()

    pubsub = SimpleNamespace(subscribe=AsyncMock(), get_message=receive, aclose=AsyncMock())
    client = SimpleNamespace(pubsub=lambda: pubsub, aclose=AsyncMock())
    created = []

    def connect(*args, **kwargs):
        created.append(client)
        return client

    monkeypatch.setenv("SHARED_REDIS_URI", "redis://localhost")
    monkeypatch.setattr(consumer.redis_async, "from_url", connect)
    task = asyncio.create_task(consumer._receive_events(lambda value: None))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(created) == 1
    assert pubsub.aclose.await_count == client.aclose.await_count == 1


def user_fsm():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=2, user_id=7654321))


@pytest.mark.parametrize("handler", [user.user_sub, user.show_keys, user.user_stats])
async def test_state_outage_is_retryable_not_missing_subscription(handler):
    callback = callback_for("screen")
    backend = SimpleNamespace(get_user_state=AsyncMock(side_effect=RuntimeError("unreachable")))
    i18n = SimpleNamespace(t=AsyncMock(side_effect=lambda key, *args, **kw: key))
    await handler(callback, user_fsm(), i18n, "en", backend)
    assert callback.answer.await_args.args[0] == "errors.service_unavailable"
    assert callback.message.edit_text.await_count == 0


async def test_key_picker_preserves_open_ended_access():
    callback = callback_for("show_keys")
    callback.message.content_type = "text"
    fsm = user_fsm()
    await fsm.update_data(open_ended_access=True)
    i18n = SimpleNamespace(
        t=AsyncMock(side_effect=lambda key, *args, **kw: "{name}" if key == "keys.list.entry" else key)
    )
    await user._render_keys_picker(callback, fsm, i18n=i18n, lang="en", clients=[{"id": "1", "inbound_tag": "test"}])
    assert (await fsm.get_data())["open_ended_access"] is True


async def test_expiry_timer_is_fenced_by_screen_generation():
    fsm = user_fsm()
    await fsm.set_state(UserStates.viewing_keys)
    await fsm.update_data(selected_key_client_id="one", key_screen_generation="new")
    message = SimpleNamespace(content_type="text", edit_text=AsyncMock())
    i18n = SimpleNamespace(t=AsyncMock(return_value="expired"))
    await user.auto_expire_keys_message(message, fsm, i18n=i18n, lang="en", client_id="one", generation="old", delay=0)
    assert message.edit_text.await_count == 0


async def test_help_acknowledges_callback():
    callback = callback_for("user_help")
    callback.message.content_type = "text"
    i18n = SimpleNamespace(t=AsyncMock(return_value="Help"))
    await user.user_help(callback, user_fsm(), i18n, "en")
    assert callback.answer.await_count == 1


@pytest.mark.parametrize("kind", ["expiry_notification", "traffic_notification"])
async def test_notification_client_name_cannot_inject_html(kind):
    from bot_events_consumer import _render_event

    rendered = []

    async def translate(key, *args, **values):
        if "email" in values:
            rendered.append(values["email"])
        return "text"

    bot = SimpleNamespace(send_message=AsyncMock())
    await _render_event(
        {"type": kind, "telegram_id": 1, "payload": {"email": '<a href="evil">name</a>'}},
        bot,
        SimpleNamespace(t=translate),
        None,
    )
    assert rendered == ["&lt;a href=&quot;evil&quot;&gt;name&lt;/a&gt;"]


async def test_empty_catalog_acknowledges_callback():
    callback = callback_for("tariffs:list")
    backend = SimpleNamespace(list_tariffs=AsyncMock(return_value=[]), get_user_state=AsyncMock(return_value={}))
    i18n = SimpleNamespace(t=AsyncMock(return_value="No tariffs"))
    await catalog.show_catalog(callback, user_fsm(), i18n, "en", backend)
    assert callback.answer.await_count == 1


async def test_failed_user_sync_cannot_mark_onboarding_complete():
    from aiogram.types import User
    from middleware import LangMiddleware

    backend = SimpleNamespace(upsert_user=AsyncMock(side_effect=RuntimeError("unreachable")))
    middleware = LangMiddleware(backend, None)
    user_record = User(id=123, is_bot=False, first_name="User", language_code="en")
    with pytest.raises(RuntimeError):
        await middleware._resolve(user_record)
    assert user_record.id not in middleware._user_cache


async def test_large_catalog_can_be_paged_without_losing_tariffs():
    callback = callback_for("tariffs:list")
    callback.message.content_type = "text"
    tariffs = [
        {"id": index, "name": "VPN" * 80 + str(index), "price_rub": 100, "period_days": 30, "items": []}
        for index in range(40)
    ]
    backend = SimpleNamespace(list_tariffs=AsyncMock(return_value=tariffs))

    async def translate(key, *args, **values):
        if key == "catalog.tariff_card.header":
            return "<b>" + values["name"] + "</b>"
        return values.get("name", key)

    fsm = user_fsm()
    await catalog.show_catalog(callback, fsm, SimpleNamespace(t=translate), "en", backend)
    assert len(callback.message.edit_text.await_args.args[0]) < 4096
    pages = (await fsm.get_data())["screen_pages"]
    all_callbacks = [
        button["callback_data"] for page in pages for row in page["markup"]["inline_keyboard"] for button in row
    ]
    assert all(f"buy:{index}" in all_callbacks for index in range(40))


def test_html_pages_preserve_nested_markup_and_all_text():
    from screens import split_html
    from html.parser import HTMLParser

    class TextParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.text = ""
            self.tags = []

        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)

        def handle_endtag(self, tag):
            assert self.tags.pop() == tag

        def handle_data(self, data):
            self.text += data

    content = "🔑 Foo &amp; bar " * 1000
    pages = split_html("<b><code>" + content + "</code></b>")
    combined = ""
    for page in pages:
        assert len(page.encode("utf-16-le")) // 2 <= 3500
        parser = TextParser()
        parser.feed(page)
        assert parser.tags == []
        combined += parser.text
    assert combined == "🔑 Foo & bar " * 1000


async def test_checkout_cancelled_before_first_step_releases_user():
    callback = callback_for("buy:1")
    i18n = SimpleNamespace(t=AsyncMock(return_value="Creating"))
    backend = SimpleNamespace(create_checkout=AsyncMock())
    try:
        await catalog.start_checkout(callback, None, i18n, "en", backend)
        await catalog.close_checkout_tasks()
        assert callback.from_user.id not in catalog._checkout_in_flight
    finally:
        catalog._checkout_in_flight.discard(callback.from_user.id)


async def test_oversized_key_is_sent_intact_as_download():
    before = asyncio.all_tasks()
    callback = callback_for("show_key_one")
    callback.message.content_type = "text"
    callback.message.delete = AsyncMock()
    callback.message.answer_document = AsyncMock(return_value=SimpleNamespace())
    fsm = user_fsm()
    i18n = SimpleNamespace(t=AsyncMock(return_value="Key"))
    link = "vless://" + "x" * 5000
    await user._show_single_link(
        callback,
        fsm,
        link,
        {"id": "one", "email": "key"},
        i18n=i18n,
        lang="en",
        back_label="Back",
        back_callback="user_home",
    )
    timers = asyncio.all_tasks() - before
    for timer in timers:
        timer.cancel()
    await asyncio.gather(*timers, return_exceptions=True)
    assert callback.message.answer_document.await_args.args[0].data == link.encode()
    assert callback.message.edit_text.await_count == 0


async def test_long_client_id_remains_selectable_within_telegram_callback_limit(monkeypatch):
    identifier = "client-" + "x" * 120
    record = {"id": identifier, "telegram_id": 7654321, "inbound_label": "Node"}
    keyboard = user_keys_list_kb([record], entry_template="{name}", back_label="Back")
    data = keyboard.inline_keyboard[0][0].callback_data
    assert len(data.encode()) <= 64
    callback = callback_for(data)
    rendered = AsyncMock()
    monkeypatch.setattr(user, "show_key_details", rendered)
    backend = SimpleNamespace(get_user_state=AsyncMock(return_value={"clients": [record]}))
    await user.user_key_selected(callback, user_fsm(), SimpleNamespace(), "en", backend)
    assert rendered.await_args.args[2]["id"] == identifier
