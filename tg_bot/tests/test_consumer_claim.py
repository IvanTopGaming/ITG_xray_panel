from unittest.mock import AsyncMock, MagicMock

import pytest
import bot_events_consumer as consumer


def _i18n():
    i18n = MagicMock()
    i18n.t = AsyncMock(return_value="text")
    return i18n


def _event(kind="expiry_1d", etype="expiry_notification"):
    return {
        "source": "node:instance",
        "id": 1,
        "type": etype,
        "telegram_id": 42,
        "payload": {
            "kind": kind,
            "email": "tg42_vless",
            "tariff_id": 7,
            "expiry_time_ms": 1753000000000,
            "node": "de1.example.com",
        },
    }


def _backend(event, claimed=True):
    backend = AsyncMock()
    backend.claim_event.return_value = {
        "claimed": claimed,
        "lang": "en",
        "renewable": True,
        "lease_token": "lease",
        "event": event,
    }
    backend.ack_event.return_value = {"acked": True}
    return backend


async def test_consumer_sends_when_claim_granted():
    bot = AsyncMock()
    event = _event()
    backend = _backend(event)
    i18n = _i18n()
    await consumer._handle(event, lambda: bot, i18n, None, backend)
    bot.send_message.assert_awaited_once()
    assert {call.args[1] for call in i18n.t.await_args_list} == {"en"}
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert "buy:7" in {button.callback_data for row in markup.inline_keyboard for button in row}
    backend.ack_event.assert_awaited_once_with("node:instance", 1, "lease", "delivered")


async def test_consumer_stays_silent_when_claim_refused():
    bot = AsyncMock()
    event = _event()
    await consumer._handle(event, lambda: bot, _i18n(), None, _backend(event, False))
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("access_generation", "new"),
        ("traffic_generation", "cycle"),
        ("client_id", "key"),
        ("inbound_tag", "in"),
        ("node", "de"),
        ("tariff_id", None),
    ],
)
async def test_identity_and_generation_reach_authoritative_claim(field, value):
    event = _event()
    event["payload"][field] = value
    backend = _backend(event)
    await consumer._handle(event, AsyncMock(), _i18n(), None, backend)
    backend.claim_event.assert_awaited_once_with(event)


async def test_consumer_defers_on_backend_failure():
    bot = AsyncMock()
    event = _event()
    backend = _backend(event)
    backend.claim_event.side_effect = RuntimeError("bot-api down")
    await consumer._handle(event, lambda: bot, _i18n(), None, backend)
    bot.send_message.assert_not_awaited()


async def test_payment_events_also_require_durable_claim():
    event = _event(etype="payment_succeeded")
    bot = AsyncMock()
    backend = _backend(event)
    await consumer._handle(event, lambda: bot, _i18n(), None, backend)
    backend.claim_event.assert_awaited_once_with(event)
    bot.send_message.assert_awaited_once()


async def test_redis_uri_accepts_rediss(monkeypatch):
    monkeypatch.setenv("SHARED_REDIS_URI", "rediss://data-tier:6379/0")
    assert consumer._redis_uri() == "rediss://data-tier:6379/0"


async def test_redis_uri_never_falls_back_to_the_rate_limit_store(monkeypatch):
    monkeypatch.delenv("SHARED_REDIS_URI", raising=False)
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "redis://local:6379/0")
    assert consumer._redis_uri() is None
