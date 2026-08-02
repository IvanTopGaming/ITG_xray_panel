"""The three screens the bot lost in phase 3c-2, rebuilt on one backend response.

Each test drives the real handler with a fake `BackendClient` and asserts on the text that reaches
Telegram. The point is not the wording — it is that the numbers and links come out of
`/bot-service/users/<id>/state` and that no HTTP call happens beyond that one.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import handlers.user as user


NODE_LINK = "vless://aaaa-bbbb@node-ams.example.com:443?security=reality#Amsterdam"
OTHER_LINK = "vless://cccc-dddd@node-fra.example.com:443?security=reality#Frankfurt"


def _record(**overrides):
    record = {
        "id": "aaaa-bbbb",
        "email": "tg42_vless-reality",
        "inbound_tag": "vless-reality",
        "inbound_label": "Amsterdam",
        "telegram_id": 42,
        "enable": True,
        "up": 1_073_741_824,
        "down": 2_147_483_648,
        "limit_bytes": 10_737_418_240,
        "expiry_time": 0,
        "tariff_id": None,
        "links": [NODE_LINK],
    }
    record.update(overrides)
    return record


def _i18n():
    i18n = MagicMock()

    async def t(key, lang, **kwargs):
        if kwargs:
            return f"[{key}:" + ",".join(f"{k}={v}" for k, v in sorted(kwargs.items())) + "]"
        return f"[{key}]"

    i18n.t = AsyncMock(side_effect=t)
    return i18n


def _callback():
    callback = MagicMock()
    callback.from_user.id = 42
    callback.from_user.first_name = "Tester"
    callback.message = MagicMock()
    callback.answer = AsyncMock()
    return callback


def _state():
    state = MagicMock()
    state.clear = AsyncMock()
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    return state


def _backend(clients, **extra):
    backend = MagicMock()
    payload = {"clients": clients, "sub_url": "https://sub.example.com/api/sub/u/tok", "expires_at_ms": 0}
    payload.update(extra)
    backend.get_user_state = AsyncMock(return_value=payload)
    return backend


@pytest.fixture
def captured(monkeypatch):
    sent = []

    async def fake_safe_edit(message, text, reply_markup=None, parse_mode="HTML"):
        sent.append({"text": text, "markup": reply_markup})
        return message

    monkeypatch.setattr(user, "safe_edit", fake_safe_edit)
    monkeypatch.setattr(user.asyncio, "create_task", lambda coro: coro.close())
    return sent


async def test_statistics_comes_from_the_state_response(captured):
    """Before: get_client_stats_aggregate 404'd and every key read "unavailable"."""

    backend = _backend([_record(), _record(id="cccc-dddd", inbound_label="Frankfurt", up=0, down=536_870_912)])

    await user.user_stats(_callback(), _state(), _i18n(), "en", backend)

    backend.get_user_state.assert_awaited_once_with(42)
    text = captured[-1]["text"]
    assert "Amsterdam" in text and "Frankfurt" in text
    assert "3.00 GB" in text, f"the first key's up+down is missing from {text!r}"
    assert "512.00 MB" in text, f"the second key's up+down is missing from {text!r}"
    assert "10.00 GB" in text, "the traffic limit is missing"


async def test_statistics_survives_a_key_with_no_traffic_fields(captured):
    """Snapshot dicts come from a node; a field the node omitted must not blank the screen."""

    backend = _backend([{"id": "x", "email": "e", "inbound_tag": "t", "inbound_label": "Minimal", "links": []}])

    await user.user_stats(_callback(), _state(), _i18n(), "en", backend)

    assert "Minimal" in captured[-1]["text"]


async def test_keys_render_the_links_from_the_record(captured):
    """Before: get_dedup_subscription_links 404'd and the screen said "no keys"."""

    await user.show_key_details(_callback(), _state(), _record(), i18n=_i18n(), lang="en")

    text = captured[-1]["text"]
    assert NODE_LINK in text, f"the share link never reached the message: {text!r}"


async def test_a_key_with_several_links_shows_all_of_them(captured):
    await user.show_key_details(
        _callback(),
        _state(),
        _record(links=[NODE_LINK, OTHER_LINK]),
        i18n=_i18n(),
        lang="en",
    )

    text = captured[-1]["text"]
    assert NODE_LINK in text and OTHER_LINK in text


async def test_a_key_with_no_links_says_so_instead_of_pretending(captured):
    """A local Client row on bot-api has no link, and inventing one from this host would be wrong."""

    await user.show_key_details(_callback(), _state(), _record(links=[]), i18n=_i18n(), lang="en")

    text = captured[-1]["text"]
    assert "vless://" not in text
    assert "keys.details.none" in text


async def test_the_qr_button_encodes_this_keys_own_link(monkeypatch, captured):
    """The server picker is gone: the record already names its server."""

    encoded = []
    monkeypatch.setattr(user, "generate_qr", lambda data: encoded.append(data) or MagicMock())

    callback = _callback()
    callback.message.delete = AsyncMock()
    callback.message.answer_photo = AsyncMock()
    state = _state()
    state.get_data = AsyncMock(return_value={"selected_key_client_id": "aaaa-bbbb"})

    await user.qr_for_key(callback, state, _backend([_record()]), _i18n(), "en")

    assert encoded == [NODE_LINK]


async def test_the_subscription_screen_offers_a_qr_of_the_subscription(monkeypatch, captured):
    """One QR for the whole subscription, the same thing the subscription page hands out."""

    encoded = []
    monkeypatch.setattr(user, "generate_qr", lambda data: encoded.append(data) or MagicMock())

    callback = _callback()
    callback.message.delete = AsyncMock()
    callback.message.answer_photo = AsyncMock()

    await user.qr_for_subscription(callback, _state(), _backend([_record()]), _i18n(), "en")

    assert encoded == ["https://sub.example.com/api/sub/u/tok"]
