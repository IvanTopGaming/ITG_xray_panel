import datetime as dt
import json
from unittest.mock import MagicMock

from panel_core.models import BotEvent
from panel_core.services import bot_events


def test_publish_stored_preserves_existing_event_identity(app, db, monkeypatch):
    event = BotEvent(type="payment_succeeded", telegram_id=42, payload={"payment_id": 9})
    db.session.add(event)
    db.session.commit()
    bus = MagicMock()
    bus.publish.return_value = 1
    monkeypatch.setattr(bot_events, "_get_redis", lambda: bus)
    bot_events.publish_stored(event)
    assert BotEvent.query.count() == 1
    assert json.loads(bus.publish.call_args.args[1])["id"] == event.id
    assert event.delivered_at is None


def test_zero_subscriber_publish_still_suppresses_delivery(app, db, monkeypatch):
    bus = MagicMock()
    bus.publish.return_value = 0
    monkeypatch.setattr(bot_events, "_get_redis", lambda: bus)
    bot_events.publish("payment_succeeded", 42, {"payment_id": 9})
    assert BotEvent.query.one().delivered_at is not None


def test_event_source_is_persisted_before_publish_and_reused(app, db, monkeypatch):
    bus = MagicMock()
    bus.publish.return_value = 1
    monkeypatch.setattr(bot_events, "_get_redis", lambda: bus)
    bot_events.publish("payment_succeeded", 42, {"payment_id": 9})
    event = BotEvent.query.one()
    first = json.loads(bus.publish.call_args.args[1])
    assert first["source"] == event.source
    assert event.source
    bot_events.publish_stored(event)
    assert json.loads(bus.publish.call_args.args[1])["source"] == first["source"]


def test_warning_marker_rolls_back_with_failed_event_enqueue(app, db, monkeypatch):
    from sqlalchemy import event as sa_event
    from panel_core.models import Client, NotificationLog
    from panel_core.services.notifications import emit_if_new

    client = Client(id="one", email="one", inbound_tag="in", telegram_id=42, expiry_time=1)
    db.session.add(client)
    db.session.commit()

    def fail_event(mapper, connection, target):
        raise RuntimeError("persist failed")

    sa_event.listen(BotEvent, "before_insert", fail_event)
    try:
        import pytest

        with pytest.raises(RuntimeError):
            emit_if_new("expiry_notification", "expired", client, {"expiry_time_ms": 1})
    finally:
        sa_event.remove(BotEvent, "before_insert", fail_event)
        db.session.rollback()
    assert NotificationLog.query.count() == 0


def _payment_event(db):
    event = bot_events.enqueue("payment_succeeded", 42, {"payment_id": 9})
    db.session.commit()
    return {"source": event.source, "id": event.id, "type": event.type, "telegram_id": 42, "payload": event.payload}


def test_delivery_survives_retry_and_rejects_old_lease_ack(app, db, monkeypatch):
    from panel_core.services import bot_delivery
    from panel_core.models import BotDelivery

    event = _payment_event(db)
    current = dt.datetime(2026, 9, 13)
    monkeypatch.setattr(bot_delivery, "_now", lambda: current)
    first = bot_delivery.claim_event(event)
    assert first["claimed"] is True
    assert BotEvent.query.one().delivered_at is not None
    assert bot_delivery.claim_event(event)["claimed"] is False
    current += dt.timedelta(seconds=121)
    second = bot_delivery.claim_event(event)
    assert second["claimed"] is True
    assert second["lease_token"] != first["lease_token"]
    assert not bot_delivery.ack_event(event["source"], event["id"], first["lease_token"], "delivered")
    assert bot_delivery.ack_event(event["source"], event["id"], second["lease_token"], "retry")
    assert BotDelivery.query.one().state == "pending"
    current += dt.timedelta(seconds=31)
    assert bot_delivery.pending_events()[0]["id"] == event["id"]
    third = bot_delivery.claim_event(event)
    assert bot_delivery.ack_event(event["source"], event["id"], third["lease_token"], "delivered")
    assert bot_delivery.claim_event(event)["claimed"] is False
    assert bot_delivery.pending_events() == []


def test_stale_generation_cannot_consume_current_warning(app, db, monkeypatch):
    from panel_core.services import bot_delivery
    from panel_core.models import BotDelivery, LinkedPanel

    now = dt.datetime(2026, 9, 13)
    expiry = int(now.replace(tzinfo=dt.timezone.utc).timestamp() * 1000) + 1800000
    db.session.add(
        LinkedPanel(
            id=1, name="node", url="https://node", federation_token="test", created_at=1, current_instance_id="instance"
        )
    )
    db.session.commit()
    client = {
        "id": "one",
        "inbound_tag": "in",
        "email": "one",
        "telegram_id": 42,
        "tariff_id": 7,
        "expiry_time": expiry,
        "access_generation": "new",
        "limit_bytes": 0,
    }
    monkeypatch.setattr(bot_delivery, "_now", lambda: now)
    monkeypatch.setattr(
        bot_delivery, "fetch_panel_snapshot_live", lambda _: {"inbounds": [{"tag": "in", "clients": [client]}]}
    )
    event = {
        "source": "node:instance",
        "id": 10,
        "created_at_ms": expiry - 1800000,
        "type": "expiry_notification",
        "telegram_id": 42,
        "payload": {
            "node": "node",
            "client_id": "one",
            "inbound_tag": "in",
            "email": "one",
            "tariff_id": 7,
            "kind": "expiry_1h",
            "access_generation": "old",
            "expiry_time_ms": expiry,
        },
    }
    monkeypatch.setattr(
        bot_delivery.FederationClient,
        "_call_reporting",
        lambda self, verb, path, **kw: event if verb == "get" else {"acked": True},
    )
    assert bot_delivery.claim_event(event)["claimed"] is False
    assert BotDelivery.query.one().detail == "stale_generation"
    event = {**event, "id": 11, "payload": {**event["payload"], "access_generation": "new"}}
    assert bot_delivery.claim_event(event)["claimed"] is True


def test_cross_node_warning_dedup_is_scoped_to_access_generation(app, db, monkeypatch):
    from panel_core.services import bot_delivery
    from panel_core.models import LinkedPanel

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    expiry = int(now.replace(tzinfo=dt.timezone.utc).timestamp() * 1000) + 1000
    for number in (1, 2):
        db.session.add(
            LinkedPanel(
                id=number,
                name=f"node{number}",
                url=f"https://node{number}",
                federation_token="test",
                created_at=1,
                current_instance_id=f"instance{number}",
            )
        )
    db.session.commit()
    active_generation = "cycle1"
    monkeypatch.setattr(
        bot_delivery,
        "fetch_panel_snapshot_live",
        lambda _: {
            "inbounds": [
                {
                    "tag": "in",
                    "clients": [
                        {
                            "id": "one",
                            "telegram_id": 42,
                            "tariff_id": 7,
                            "access_generation": active_generation,
                            "expiry_time": expiry,
                        }
                    ],
                }
            ]
        },
    )
    current_event = {}
    monkeypatch.setattr(
        bot_delivery.FederationClient,
        "_call_reporting",
        lambda self, verb, path, **kw: current_event if verb == "get" else {"acked": True},
    )

    def claim(number, origin):
        nonlocal current_event
        current_event = {
            "source": f"node:instance{number}",
            "id": origin,
            "type": "expiry_notification",
            "telegram_id": 42,
            "payload": {
                "client_id": "one",
                "inbound_tag": "in",
                "tariff_id": 7,
                "kind": "expiry_1h",
                "access_generation": active_generation,
                "expiry_time_ms": expiry,
            },
        }
        return bot_delivery.claim_event(current_event)

    assert claim(1, 10)["claimed"] is True
    assert claim(2, 10)["reason"] == "duplicate_warning"
    active_generation = "cycle2"
    assert claim(2, 11)["claimed"] is True


def test_event_api_requires_auth_and_completes_a_persisted_delivery(app, db):
    from panel_core.api.bot_delivery import bp
    from panel_core.models import BotDelivery, SystemSetting

    app.register_blueprint(bp, url_prefix="/api")
    db.session.add(SystemSetting(key="bot_service_token", value="test-token"))
    value = _payment_event(db)
    client = app.test_client()
    assert client.post("/api/bot-service/events/claim", json=value).status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    response = client.post("/api/bot-service/events/claim", json=value, headers=headers)
    assert response.status_code == 200
    verdict = response.get_json()
    assert verdict["claimed"] is True
    response = client.post(
        "/api/bot-service/events/ack",
        json={
            "source": value["source"],
            "id": value["id"],
            "lease_token": verdict["lease_token"],
            "outcome": "delivered",
        },
        headers=headers,
    )
    assert response.get_json() == {"acked": True}
    assert BotDelivery.query.one().state == "delivered"
    assert client.get("/api/bot-service/events/pending", headers=headers).get_json() == {"events": []}


def test_health_exposes_stalled_inbox_even_after_outbox_ack(app, db):
    from panel_core.models import BotDelivery
    from panel_core.services.health import collect

    created = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=10)
    db.session.add(
        BotDelivery(source="shared:test", event_id=1, event={}, state="pending", created_at=created, source_acked=True)
    )
    db.session.add(
        BotDelivery(source="shared:test", event_id=2, event={}, state="review", created_at=created, source_acked=True)
    )
    db.session.commit()
    reading = collect()["event_delivery"]
    assert reading["available"] is True
    assert reading["pending"] == reading["review"] == 1
    assert reading["oldest_pending_ms"] > 0
    assert reading["needs_attention"] is True


def test_outbox_replays_until_inbox_persistence(app, db, monkeypatch):
    from panel_core.jobs import notifications as jobs
    from panel_core.services import bot_delivery

    bus = MagicMock()
    bus.publish.return_value = 1
    monkeypatch.setattr(bot_events, "_get_redis", lambda: bus)
    monkeypatch.setattr(jobs, "_get_redis", lambda: bus)
    bot_events.publish("payment_succeeded", 42, {"payment_id": 9})
    row = BotEvent.query.one()
    row.created_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=2)
    db.session.commit()
    jobs.replay_undelivered_bot_events()
    assert bus.publish.call_count == 2
    assert row.delivered_at is None
    value = json.loads(bus.publish.call_args.args[1])
    assert bot_delivery.claim_event(value)["claimed"] is True
    jobs.replay_undelivered_bot_events()
    assert bus.publish.call_count == 2


def test_missing_legacy_generation_is_reviewed_before_claim(app, db, monkeypatch):
    from panel_core.models import Client, BotDelivery
    from panel_core.services import bot_delivery

    expiry = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) + 1000
    db.session.add(Client(id="legacy", email="legacy", inbound_tag="in", telegram_id=42, expiry_time=expiry))
    row = bot_events.enqueue(
        "expiry_notification",
        42,
        {"kind": "expiry_1h", "client_id": "legacy", "inbound_tag": "in", "expiry_time_ms": expiry},
    )
    db.session.commit()
    value = bot_delivery._envelope(row)
    value.pop("created_at_ms")
    monkeypatch.setattr(bot_delivery, "_canonical_event", lambda event: value)
    assert bot_delivery.claim_event(value)["claimed"] is False
    assert BotDelivery.query.one().state == "review"
    assert BotDelivery.query.one().dedup_key is None


def test_traffic_generations_get_independent_delivery_claims(app, db):
    from panel_core.models import Client
    from panel_core.services import bot_delivery

    client = Client(
        id="traffic",
        email="traffic",
        inbound_tag="in",
        telegram_id=42,
        tariff_id=7,
        limit_bytes=100,
        up=80,
        traffic_generation="cycle1",
    )
    db.session.add(client)
    db.session.commit()
    for generation in ("cycle1", "cycle2"):
        client.traffic_generation = generation
        event = bot_events.enqueue(
            "traffic_notification",
            42,
            {
                "kind": "traffic_80",
                "client_id": "traffic",
                "inbound_tag": "in",
                "tariff_id": 7,
                "traffic_generation": generation,
            },
        )
        db.session.commit()
        assert bot_delivery.claim_event(bot_delivery._envelope(event))["claimed"] is True


def test_federation_ack_uses_origin_pair_after_local_id_changes(app, db, monkeypatch):
    from panel_core.api.federation import bp
    from panel_core.models import FederationConfig

    monkeypatch.setenv("PANEL_ROLE", "worker")
    app.register_blueprint(bp, url_prefix="/api")
    db.session.get(FederationConfig, 1).federation_token = "node-token"
    row = BotEvent(id=99, source="node:old", origin_event_id=7, type="traffic_notification", telegram_id=42, payload={})
    db.session.add(row)
    db.session.commit()
    client = app.test_client()
    headers = {"X-Federation-Token": "node-token"}
    assert client.post("/api/federation/events/7/ack", json={"source": "node:old"}).status_code == 401
    response = client.get("/api/federation/events/7", query_string={"source": "node:old"}, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["id"] == 7
    assert (
        client.post("/api/federation/events/7/ack", json={"source": "node:other"}, headers=headers).status_code == 404
    )
    assert db.session.get(BotEvent, 99).delivered_at is None
    assert client.post("/api/federation/events/7/ack", json={"source": "node:old"}, headers=headers).get_json() == {
        "acked": True
    }
    db.session.expire_all()
    assert db.session.get(BotEvent, 99).delivered_at is not None


def test_delayed_bus_copy_cannot_revive_zero_subscriber_suppression(app, db, monkeypatch):
    from panel_core.services import bot_delivery

    bus = MagicMock()
    bus.publish.return_value = 0
    monkeypatch.setattr(bot_events, "_get_redis", lambda: bus)
    bot_events.publish("payment_succeeded", 42, {"payment_id": 9})
    delayed_copy = json.loads(bus.publish.call_args.args[1])
    verdict = bot_delivery.claim_event(delayed_copy)
    assert verdict["claimed"] is False
    assert verdict["reason"] == "outbox_suppressed"
