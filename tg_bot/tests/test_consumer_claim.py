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


def _langs(i18n):
    return {call.args[1] for call in i18n.t.await_args_list}


def _callbacks(markup):
    return {button.callback_data for row in markup.inline_keyboard for button in row}


async def test_consumer_sends_when_claim_granted():
    bot = AsyncMock()
    backend = AsyncMock()
    i18n = _i18n()
    backend.claim_notification.return_value = {"claimed": True, "lang": "en", "renewable": True}

    await consumer._handle(_event(), lambda: bot, i18n, None, backend)

    bot.send_message.assert_awaited_once()
    assert _langs(i18n) == {"en"}
    assert "buy:7" in _callbacks(bot.send_message.await_args.kwargs["reply_markup"])


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
    event["payload"].update(
        {"used_bytes": 80, "limit_bytes": 100, "inbound_tag": "vless-reality", "cycle": 1753000000000}
    )

    await consumer._handle(event, lambda: bot, _i18n(), None, backend)

    assert backend.claim_notification.call_args.kwargs["scope"] == (
        "de1.example.com/vless-reality/tg42_vless/1753000000000"
    )


async def test_traffic_scope_changes_after_a_monthly_reset():
    bot = AsyncMock()
    backend = AsyncMock()
    backend.claim_notification.return_value = {"claimed": True, "lang": "ru", "renewable": False}
    scopes = []
    for cycle in (1753000000000, 1755678400000):
        event = _event(kind="traffic_80", etype="traffic_notification")
        event["payload"].update({"used_bytes": 80, "limit_bytes": 100, "inbound_tag": "vless-reality", "cycle": cycle})
        await consumer._handle(event, lambda: bot, _i18n(), None, backend)
        scopes.append(backend.claim_notification.call_args.kwargs["scope"])

    assert scopes[0] != scopes[1]


async def test_expiry_scope_distinguishes_tariffless_clients_of_one_user():
    bot = AsyncMock()
    backend = AsyncMock()
    backend.claim_notification.return_value = {"claimed": True, "lang": "ru", "renewable": False}
    scopes = []
    for email in ("admin_de", "admin_nl"):
        event = _event()
        event["payload"].update({"tariff_id": None, "inbound_tag": "vless-reality", "email": email})
        await consumer._handle(event, lambda: bot, _i18n(), None, backend)
        scopes.append(backend.claim_notification.call_args.kwargs["scope"])

    assert scopes == ["de1.example.com/vless-reality/admin_de", "de1.example.com/vless-reality/admin_nl"]


async def test_expiry_scope_stays_empty_for_a_real_tariff_on_every_node():
    bot = AsyncMock()
    backend = AsyncMock()
    backend.claim_notification.return_value = {"claimed": True, "lang": "ru", "renewable": False}
    scopes = []
    for node in ("de1.example.com", "nl1.example.com"):
        event = _event()
        event["payload"].update({"node": node, "inbound_tag": "vless-reality", "email": f"tg42_{node}"})
        await consumer._handle(event, lambda: bot, _i18n(), None, backend)
        scopes.append(backend.claim_notification.call_args.kwargs["scope"])

    assert scopes == ["", ""]


def test_scope_never_exceeds_the_column_width():
    payload = {
        "node": "n" * 253,
        "inbound_tag": "t" * 50,
        "email": "e" * 100,
        "cycle": 1753000000000,
    }
    scope = consumer._claim_scope("traffic_notification", payload)

    assert len(scope) == 200
    assert scope != consumer._claim_scope("traffic_notification", {**payload, "cycle": 1755678400000})


async def test_consumer_sends_on_backend_failure():
    bot = AsyncMock()
    backend = AsyncMock()
    i18n = _i18n()
    backend.claim_notification.side_effect = RuntimeError("bot-api down")

    await consumer._handle(_event(), lambda: bot, i18n, None, backend)

    bot.send_message.assert_awaited_once()
    assert _langs(i18n) == {"ru"}
    assert not any(cb.startswith("buy:") for cb in _callbacks(bot.send_message.await_args.kwargs["reply_markup"]))


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
