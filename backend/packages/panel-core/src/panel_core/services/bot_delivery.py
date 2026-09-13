import datetime as dt
import hashlib
import json
import logging
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from panel_core.extensions import db
from panel_core.models import BotDelivery, BotEvent, Client, LinkedPanel, TelegramUser, UserTariffAccess
from panel_core.services.notifications import _is_renewable, _lookup_lang, evaluate_expiry, evaluate_traffic
from panel_core.services.panel_proxy import FederationClient, fetch_panel_snapshot_live


logger = logging.getLogger(__name__)
_LEASE_SECONDS = 120
_TERMINAL = ("delivered", "permanent", "review", "suppressed")
_WARNINGS = ("expiry_notification", "traffic_notification")


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _envelope(row):
    from panel_core.services.bot_events import event_source

    return {
        "source": row.source,
        "sender": event_source(),
        "id": row.origin_event_id,
        "type": row.type,
        "telegram_id": row.telegram_id,
        "payload": row.payload,
        "created_at_ms": int(row.created_at.replace(tzinfo=dt.timezone.utc).timestamp() * 1000),
        "outbox_acked": row.delivered_at is not None,
    }


def _source_panel(event):
    if event.get("_panel_id") is not None:
        panel = LinkedPanel.query.filter_by(id=event["_panel_id"], enable=True).first()
        if panel is not None:
            return panel
    source = event["source"]
    identity = str(event.get("sender") or source).removeprefix("node:")
    panel = LinkedPanel.query.filter(
        LinkedPanel.enable.is_(True),
        or_(LinkedPanel.current_instance_id == identity, LinkedPanel.superseded_instance_id == identity),
    ).first()
    if panel is not None:
        return panel
    node = str(event.get("payload", {}).get("node") or "")
    for candidate in LinkedPanel.query.filter_by(enable=True).all():
        if node and urlsplit(candidate.url).hostname == node:
            return candidate
    raise ValueError("unknown_event_source")


def _canonical_event(event):
    source = event.get("source")
    event_id = event.get("id")
    if not isinstance(source, str) or not source or len(source) > 128:
        raise ValueError("event source is required")
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
        raise ValueError("positive event id is required")
    if source.startswith("shared:"):
        row = BotEvent.query.filter_by(origin_event_id=event_id, source=source).first()
        if row is None:
            raise ValueError("unknown_event_source")
        return _envelope(row)
    if not source.startswith("node:"):
        raise ValueError("unknown_event_source")
    panel = _source_panel(event)
    client = FederationClient(panel.url, panel.federation_token)
    canonical = client._call_reporting(
        "get", f"/api/federation/events/{event_id}", params={"source": source}, timeout=(2, 5)
    )
    if canonical.get("id") != event_id or canonical.get("source") != source:
        raise ValueError("event source mismatch")
    canonical = {**canonical, "_panel_id": panel.id}
    return canonical


def _ack_source(delivery):
    if delivery.source_acked:
        return
    if delivery.source.startswith("shared:"):
        BotEvent.query.filter_by(origin_event_id=delivery.event_id, source=delivery.source).update(
            {"delivered_at": _now()}, synchronize_session=False
        )
    else:
        try:
            panel = _source_panel(delivery.event)
            result = FederationClient(panel.url, panel.federation_token)._call_reporting(
                "post",
                f"/api/federation/events/{delivery.event_id}/ack",
                json={"source": delivery.source},
                timeout=(2, 5),
            )
            if result.get("acked") is not True:
                return
        except Exception as exc:
            logger.warning("event outbox acknowledgment failed for delivery=%s: %s", delivery.id, type(exc).__name__)
            return
    delivery.source_acked = True
    db.session.commit()


def _persist(event):
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    source = event.get("source")
    event_id = event.get("id")
    if (
        not isinstance(source, str)
        or not source
        or len(source) > 128
        or not isinstance(event_id, int)
        or isinstance(event_id, bool)
        or not 0 < event_id < 2**63
    ):
        raise ValueError("event source and numeric id are required")
    delivery = BotDelivery.query.filter_by(source=source, event_id=event_id).first()
    if delivery is None:
        canonical = _canonical_event(event)
        if not isinstance(canonical.get("payload"), dict) or len(json.dumps(canonical)) > 65536:
            raise ValueError("invalid event payload")
        delivery = BotDelivery(source=source, event_id=event_id, event=canonical, next_attempt_at=_now())
        if canonical.get("outbox_acked"):
            delivery.state = "suppressed"
            delivery.detail = "outbox_suppressed"
        db.session.add(delivery)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            delivery = BotDelivery.query.filter_by(source=source, event_id=event_id).one()
    _ack_source(delivery)
    return delivery


def _live_client(event):
    payload = event["payload"]
    if event["source"].startswith("shared:"):
        row = db.session.get(Client, str(payload.get("client_id") or ""))
        return row.to_dict() if row is not None else None
    snapshot = fetch_panel_snapshot_live(_source_panel(event).id)
    for inbound in snapshot.get("inbounds", []):
        if inbound.get("tag") != payload.get("inbound_tag"):
            continue
        for client in inbound.get("clients", []):
            if client.get("id") == payload.get("client_id"):
                return {**client, "inbound_tag": inbound["tag"]}
    return None


def _warning_key(event):
    payload = event["payload"]
    client = _live_client(event)
    if client is None:
        return None, "client_missing"
    if client.get("telegram_id") != event.get("telegram_id") or client.get("tariff_id") != payload.get("tariff_id"):
        return None, "client_changed"
    traffic = event["type"] == "traffic_notification"
    field = "traffic_generation" if traffic else "access_generation"
    generation = str(payload.get(field) or "")
    current_generation = str(client.get(field) or "")
    if generation != current_generation:
        return None, "stale_generation"
    now_ms = int(_now().replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    if not generation:
        created_at = event.get("created_at_ms")
        if not isinstance(created_at, int) or not 0 < created_at <= now_ms + 60000:
            return None, "legacy_generation_unknown"
        cycle = int(client.get("last_reset_time") or 0)
        if cycle > created_at:
            return None, "stale_generation"
        if traffic:
            if payload.get("cycle") != cycle:
                return None, "legacy_generation_unknown"
            generation = f"legacy:{cycle}"
        else:
            expiry = client.get("expiry_time")
            if not isinstance(expiry, int) or expiry != payload.get("expiry_time_ms"):
                return None, "stale_expiry"
            generation = f"legacy:{expiry}"
    value = SimpleNamespace(
        expiry_time=client.get("expiry_time"),
        limit_bytes=client.get("limit_bytes"),
        up=client.get("up"),
        down=client.get("down"),
    )
    actual_kind = evaluate_traffic(value) if traffic else evaluate_expiry(value, now_ms)
    if actual_kind != payload.get("kind"):
        return None, "warning_no_longer_current"
    parts = [event["telegram_id"], payload.get("tariff_id") or 0, payload["kind"], generation]
    if traffic or not payload.get("tariff_id"):
        parts.extend((event["source"], payload.get("inbound_tag"), payload.get("client_id")))
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest(), None


def _verdict(delivery, claimed=False, lease_token=None):
    event = delivery.event
    telegram_id = event.get("telegram_id")
    tariff_id = event["payload"].get("tariff_id")
    renewable = False
    if telegram_id is not None and tariff_id:
        permanent = UserTariffAccess.query.filter_by(telegram_id=telegram_id, billing="free", access_until=None).first()
        renewable = permanent is None and _is_renewable(tariff_id, telegram_id, {})
    return {
        "claimed": claimed,
        "lease_token": lease_token,
        "lease_seconds": _LEASE_SECONDS,
        "lang": _lookup_lang(telegram_id, {}) if telegram_id is not None else "ru",
        "renewable": renewable,
        "state": delivery.state,
        "reason": delivery.detail,
        "event": event,
    }


def claim_event(event):
    delivery = _persist(event)
    now = _now()
    if delivery.state in _TERMINAL or delivery.next_attempt_at > now:
        return _verdict(delivery)
    if delivery.state == "leased" and delivery.lease_until and delivery.lease_until > now:
        return _verdict(delivery)
    canonical = delivery.event
    user = (
        db.session.get(TelegramUser, canonical.get("telegram_id")) if canonical.get("telegram_id") is not None else None
    )
    dedup_key = None
    reason = "user_blocked" if user is not None and user.blocked else None
    if reason is None and canonical["type"] in _WARNINGS:
        try:
            dedup_key, reason = _warning_key(canonical)
        except Exception as exc:
            logger.warning("live warning validation failed for delivery=%s: %s", delivery.id, type(exc).__name__)
            delivery.next_attempt_at = now + dt.timedelta(seconds=30)
            delivery.detail = "live_validation_unavailable"
            db.session.commit()
            return _verdict(delivery)
    if reason is not None:
        delivery.state = "review" if reason == "legacy_generation_unknown" else "suppressed"
        delivery.detail = reason
        db.session.commit()
        return _verdict(delivery)
    token = uuid.uuid4().hex
    try:
        updated = BotDelivery.query.filter(
            BotDelivery.id == delivery.id,
            BotDelivery.state.in_(("pending", "leased")),
            or_(BotDelivery.lease_until.is_(None), BotDelivery.lease_until <= now),
        ).update(
            {
                "state": "leased",
                "lease_token": token,
                "lease_until": now + dt.timedelta(seconds=_LEASE_SECONDS),
                "dedup_key": dedup_key,
                "attempts": BotDelivery.attempts + 1,
                "updated_at": now,
                "detail": "",
            },
            synchronize_session=False,
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        delivery = db.session.get(BotDelivery, delivery.id)
        delivery.state = "suppressed"
        delivery.detail = "duplicate_warning"
        db.session.commit()
        return _verdict(delivery)
    db.session.refresh(delivery)
    return _verdict(delivery, bool(updated), token if updated else None)


def ack_event(source, event_id, lease_token, outcome):
    if outcome not in ("delivered", "permanent_failure", "retry"):
        raise ValueError("invalid delivery outcome")
    if (
        not isinstance(source, str)
        or not source
        or len(source) > 128
        or not isinstance(event_id, int)
        or isinstance(event_id, bool)
        or not 0 < event_id < 2**63
    ):
        raise ValueError("event source and numeric id are required")
    if not isinstance(lease_token, str) or not lease_token or len(lease_token) > 64:
        raise ValueError("lease token is required")
    now = _now()
    state = {"delivered": "delivered", "permanent_failure": "permanent", "retry": "pending"}[outcome]
    updated = BotDelivery.query.filter(
        BotDelivery.source == source,
        BotDelivery.event_id == event_id,
        BotDelivery.state == "leased",
        BotDelivery.lease_token == lease_token,
        BotDelivery.lease_until > now,
    ).update(
        {
            "state": state,
            "lease_until": None,
            "next_attempt_at": now + dt.timedelta(seconds=5),
            "updated_at": now,
            "detail": "telegram_rejected" if outcome == "permanent_failure" else "",
        },
        synchronize_session=False,
    )
    db.session.commit()
    if updated:
        return True
    row = BotDelivery.query.filter_by(source=source, event_id=event_id, lease_token=lease_token, state=state).first()
    return row is not None


def pending_events():
    now = _now()
    rows = (
        BotDelivery.query.filter(
            BotDelivery.state.in_(("pending", "leased")),
            BotDelivery.next_attempt_at <= now,
            or_(BotDelivery.lease_until.is_(None), BotDelivery.lease_until <= now),
        )
        .order_by(BotDelivery.id)
        .limit(100)
        .all()
    )
    return [row.event for row in rows]
