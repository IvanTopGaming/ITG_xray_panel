from unittest.mock import AsyncMock, MagicMock

import bot_events_consumer as consumer


def _i18n():
    i18n = MagicMock()
    i18n.t = AsyncMock(return_value="text")
    return i18n


def _event(kind="expiry_1d", etype="expiry_notification"):
    return {
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


async def test_consumer_sends_when_claim_granted():
    bot = AsyncMock()
    backend = AsyncMock()
    backend.claim_notification.return_value = {"claimed": True, "lang": "en", "renewable": True}

    await consumer._handle(_event(), lambda: bot, _i18n(), None, backend)

    bot.send_message.assert_awaited_once()


async def test_consumer_stays_silent_when_claim_refused():
    bot = AsyncMock()
    backend = AsyncMock()
    backend.claim_notification.return_value = {"claimed": False, "lang": "ru", "renewable": False}

    await consumer._handle(_event(), lambda: bot, _i18n(), None, backend)

    bot.send_message.assert_not_awaited()


async def test_expiry_claim_scope_is_empty():
    bot = AsyncMock()
    backend = AsyncMock()
    backend.claim_notification.return_value = {"claimed": True, "lang": "ru", "renewable": False}

    await consumer._handle(_event(), lambda: bot, _i18n(), None, backend)

    assert backend.claim_notification.call_args.kwargs["scope"] == ""


async def test_traffic_claim_scope_includes_node_tag_and_email():
    bot = AsyncMock()
    backend = AsyncMock()
    backend.claim_notification.return_value = {"claimed": True, "lang": "ru", "renewable": False}
    event = _event(kind="traffic_80", etype="traffic_notification")
    event["payload"].update({"used_bytes": 80, "limit_bytes": 100, "inbound_tag": "vless-reality"})

    await consumer._handle(event, lambda: bot, _i18n(), None, backend)

    assert backend.claim_notification.call_args.kwargs["scope"] == ("de1.example.com/vless-reality/tg42_vless")


async def test_consumer_sends_on_backend_failure():
    bot = AsyncMock()
    backend = AsyncMock()
    backend.claim_notification.side_effect = RuntimeError("bot-api down")

    await consumer._handle(_event(), lambda: bot, _i18n(), None, backend)

    bot.send_message.assert_awaited_once()


async def test_payment_events_do_not_claim():
    bot = AsyncMock()
    backend = AsyncMock()
    event = {
        "type": "payment_succeeded",
        "telegram_id": 42,
        "payload": {"lang": "ru", "expires_at_ms": 1753000000000},
    }

    await consumer._handle(event, lambda: bot, _i18n(), None, backend)

    backend.claim_notification.assert_not_awaited()
    bot.send_message.assert_awaited_once()


async def test_redis_uri_accepts_rediss(monkeypatch):
    monkeypatch.setenv("BOT_EVENTS_REDIS_URI", "rediss://data-tier:6379/0")
    assert consumer._redis_uri() == "rediss://data-tier:6379/0"


async def test_redis_uri_falls_back_to_ratelimit(monkeypatch):
    monkeypatch.delenv("BOT_EVENTS_REDIS_URI", raising=False)
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "redis://local:6379/0")
    assert consumer._redis_uri() == "redis://local:6379/0"
