from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import bot_events_consumer as consumer


def event():
    return {
        "source": "shared:one",
        "id": 1,
        "type": "payment_succeeded",
        "telegram_id": 42,
        "payload": {"expires_at_ms": 0},
    }


def backend_for(value):
    return SimpleNamespace(
        claim_event=AsyncMock(
            return_value={"claimed": True, "lease_token": "owner", "lang": "en", "renewable": False, "event": value}
        ),
        ack_event=AsyncMock(return_value={"acked": True}),
    )


async def test_send_failure_releases_delivery_for_retry():
    value = event()
    backend = backend_for(value)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError("network"), None]))
    i18n = SimpleNamespace(t=AsyncMock(return_value="text"))
    await consumer._handle(value, bot, i18n, None, backend)
    assert backend.ack_event.await_args.args[-1] == "retry"
    await consumer._handle(value, bot, i18n, None, backend)
    assert backend.ack_event.await_args.args[-1] == "delivered"
    assert bot.send_message.await_count == 2


async def test_backend_outage_does_not_bypass_durable_claim():
    value = event()
    backend = backend_for(value)
    backend.claim_event.side_effect = RuntimeError("unavailable")
    bot = SimpleNamespace(send_message=AsyncMock())
    await consumer._handle(value, bot, SimpleNamespace(t=AsyncMock(return_value="text")), None, backend)
    assert bot.send_message.await_count == 0


async def test_ack_failure_retries_ack_without_resending(monkeypatch):
    value = event()
    backend = backend_for(value)
    backend.ack_event.side_effect = [RuntimeError("lost ack"), {"acked": True}]
    bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(consumer.asyncio, "sleep", AsyncMock())
    await consumer._handle(value, bot, SimpleNamespace(t=AsyncMock(return_value="text")), None, backend)
    assert backend.ack_event.await_count == 2
    assert bot.send_message.await_count == 1


async def test_a_slow_user_does_not_block_other_deliveries(monkeypatch):
    first_waiting = asyncio.Event()
    second_delivered = asyncio.Event()
    release_first = asyncio.Event()
    values = [event(), {**event(), "id": 2, "telegram_id": 43}]

    async def claim(value):
        return {"claimed": True, "lease_token": str(value["id"]), "lang": "en", "renewable": False, "event": value}

    async def send(telegram_id, *args, **kwargs):
        if telegram_id == 42:
            first_waiting.set()
            await release_first.wait()
        else:
            second_delivered.set()

    async def receive(enqueue):
        for value in values:
            enqueue(value)
        await asyncio.Event().wait()

    monkeypatch.setattr(consumer, "_receive_events", receive)
    backend = SimpleNamespace(
        claim_event=claim, ack_event=AsyncMock(return_value={"acked": True}), pending_events=AsyncMock(return_value=[])
    )
    task = asyncio.create_task(
        consumer.run_consumer(
            SimpleNamespace(send_message=send), SimpleNamespace(t=AsyncMock(return_value="text")), backend=backend
        )
    )
    try:
        await asyncio.wait_for(first_waiting.wait(), 1)
        await asyncio.wait_for(second_delivered.wait(), 1)
        assert not release_first.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_text_change_keeps_its_target_language_after_claim():
    value = {"source": "shared:one", "id": 2, "type": "texts_changed", "telegram_id": None, "payload": {"lang": "ru"}}
    backend = backend_for(value)
    i18n = SimpleNamespace(invalidate=AsyncMock())
    await consumer._handle(value, SimpleNamespace(), i18n, None, backend)
    i18n.invalidate.assert_awaited_once_with("ru")
