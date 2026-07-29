"""§72: pressing "buy" answers Telegram before YooKassa is asked anything.

`create_checkout` on bot-api calls YooKassa with an 8-second timeout and one retry on the same
idempotence key, so a degraded YooKassa keeps it for up to ~16 seconds. That call used to sit inside
the callback handler, with `callback.answer()` reached only afterwards -- the button in Telegram spun
for the whole time and the user could not tell whether the press had registered.

The handler now answers first, takes the catalogue keyboard away (so the same press cannot be made
twice) and builds the invoice in a background task that turns the very same message into either the
pay screen or an error.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import handlers.catalog as catalog


@pytest.fixture(autouse=True)
def _clean_module_state():
    catalog._checkout_in_flight.clear()
    catalog._checkout_tasks.clear()
    yield
    catalog._checkout_in_flight.clear()
    catalog._checkout_tasks.clear()


def _i18n():
    i18n = MagicMock()

    async def t(key, lang, **kwargs):
        return f"[{key}]"

    i18n.t = AsyncMock(side_effect=t)
    return i18n


def _callback(log, *, tariff_id=3):
    callback = MagicMock()
    callback.data = f"buy:{tariff_id}"
    callback.from_user.id = 42

    edited = MagicMock()
    edited.chat.id = 1000
    edited.message_id = 777

    async def answer(*args, **kwargs):
        log.append("answer")

    async def edit_reply_markup(*args, **kwargs):
        log.append("clear_keyboard")

    async def edit_text(*args, **kwargs):
        log.append(("edit_text", args[0] if args else kwargs.get("text"), kwargs.get("reply_markup")))
        return edited

    callback.answer = AsyncMock(side_effect=answer)
    callback.message = MagicMock()
    callback.message.edit_reply_markup = AsyncMock(side_effect=edit_reply_markup)
    callback.message.edit_text = AsyncMock(side_effect=edit_text)
    return callback


def _backend(log, *, result=None, error=None, delay=0.0):
    backend = MagicMock()

    async def create_checkout(telegram_id, tariff_id, lang):
        log.append("create_checkout")
        if delay:
            await asyncio.sleep(delay)
        if error is not None:
            raise error
        return result or {
            "payment_id": 11,
            "amount_rub": 150,
            "confirmation_url": "https://yookassa.test/pay/abc",
        }

    backend.create_checkout = AsyncMock(side_effect=create_checkout)
    backend.set_payment_chat_coords = AsyncMock(return_value=None)
    return backend


async def _drain():
    pending = list(catalog._checkout_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _http_error(status, body):
    request = httpx.Request("POST", "http://bot-api/api/billing/checkout")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("bad", request=request, response=response)


async def test_telegram_is_answered_before_yookassa_is_ever_asked():
    log = []
    callback = _callback(log)

    await catalog.start_checkout(callback, MagicMock(), _i18n(), "ru", _backend(log, delay=0.05))

    assert log[0] == "answer", (
        f"the callback was answered after {log[0]!r}. Everything before the answer is time the user "
        f"spends watching the button spin. Order was {log!r}."
    )
    assert "create_checkout" not in log, (
        "the invoice was still built inside the handler; the handler must return before YooKassa is called"
    )

    await _drain()
    assert "create_checkout" in log, "the background task never built the invoice"


async def test_the_catalogue_keyboard_is_taken_away_before_the_invoice_is_built():
    log = []
    callback = _callback(log)

    await catalog.start_checkout(callback, MagicMock(), _i18n(), "ru", _backend(log))
    await _drain()

    assert log.index("clear_keyboard") < log.index("create_checkout"), (
        f"the buy buttons stayed live while the invoice was being built, so a second press would open a "
        f"second payment. Order was {log!r}."
    )


async def test_the_pay_button_replaces_the_catalogue_when_yookassa_replies():
    log = []
    callback = _callback(log)
    backend = _backend(log)

    await catalog.start_checkout(callback, MagicMock(), _i18n(), "ru", backend)
    await _drain()

    edits = [entry for entry in log if isinstance(entry, tuple) and entry[0] == "edit_text"]
    assert edits, "the message was never turned into the pay screen"
    markup = edits[-1][2]
    urls = [button.url for row in markup.inline_keyboard for button in row if button.url]
    assert urls == ["https://yookassa.test/pay/abc"], f"the pay link is missing from the keyboard: {markup!r}"

    backend.set_payment_chat_coords.assert_awaited_once()
    kwargs = backend.set_payment_chat_coords.await_args.kwargs
    assert kwargs["chat_id"] == 1000 and kwargs["message_id"] == 777, (
        "the payment lost its chat coordinates, so payment_succeeded cannot edit the right message"
    )


async def test_a_refused_tariff_is_explained_in_the_message():
    """The callback is already answered by then, so the toast the old code used is no longer available."""

    log = []
    callback = _callback(log)
    backend = _backend(log, error=_http_error(400, {"error": "tariff_not_available"}))

    await catalog.start_checkout(callback, MagicMock(), _i18n(), "ru", backend)
    await _drain()

    edits = [entry for entry in log if isinstance(entry, tuple) and entry[0] == "edit_text"]
    assert edits, "a refused checkout left the user staring at a keyboard-less catalogue with no explanation"
    assert edits[-1][1] == "[catalog.tariff_not_available]", f"wrong text shown: {edits[-1][1]!r}"


async def test_a_broken_backend_is_explained_in_the_message():
    log = []
    callback = _callback(log)
    backend = _backend(log, error=RuntimeError("bot-api down"))

    await catalog.start_checkout(callback, MagicMock(), _i18n(), "ru", backend)
    await _drain()

    edits = [entry for entry in log if isinstance(entry, tuple) and entry[0] == "edit_text"]
    assert edits, "a failed checkout said nothing at all"
    assert edits[-1][1] == "[errors.checkout_unavailable]", f"wrong text shown: {edits[-1][1]!r}"


async def test_two_presses_in_the_same_window_create_one_invoice():
    """Answering early widens the window in which a second press is possible. It must stay one payment."""

    log = []
    backend = _backend(log, delay=0.05)

    first = _callback(log)
    second = _callback(log)
    await catalog.start_checkout(first, MagicMock(), _i18n(), "ru", backend)
    await catalog.start_checkout(second, MagicMock(), _i18n(), "ru", backend)
    await _drain()

    assert backend.create_checkout.await_count == 1, (
        f"two presses produced {backend.create_checkout.await_count} invoices. One press must mean one "
        "payment row and one YooKassa idempotence key."
    )
    assert second.answer.await_count == 1, "the second press was left unanswered and would spin"


async def test_the_next_press_works_once_the_first_one_finished():
    log = []
    backend = _backend(log)

    await catalog.start_checkout(_callback(log), MagicMock(), _i18n(), "ru", backend)
    await _drain()
    await catalog.start_checkout(_callback(log), MagicMock(), _i18n(), "ru", backend)
    await _drain()

    assert backend.create_checkout.await_count == 2, (
        "the in-flight guard never released: a user who bought once could not buy again"
    )
