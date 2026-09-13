from __future__ import annotations

import datetime as dt
import logging
import time
from sqlalchemy import inspect

from panel_core.extensions import db
from panel_core.models import BotEvent
from panel_core.services import bot_events  # noqa: F401 — re-exported for notification tests to patch
from panel_core.services.bot_events import _get_redis  # noqa: F401 — re-exported for replay job tests to patch
from panel_core.services.notifications import (  # noqa: F401 — re-exported under the original names
    emit_if_new,
    evaluate_expiry,
    evaluate_traffic,
)
from panel_core.services.supersede import is_superseded

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def replay_undelivered_bot_events() -> None:

    import json as _json

    if is_superseded():
        return

    redis_client = _get_redis()
    if redis_client is None:
        return

    cutoff = dt.datetime.utcnow() - dt.timedelta(seconds=30)
    undelivered = (
        BotEvent.query.filter(
            BotEvent.delivered_at.is_(None),
            BotEvent.created_at < cutoff,
        )
        .order_by(BotEvent.id.asc())
        .limit(200)
        .all()
    )
    if not undelivered:
        return

    now = dt.datetime.utcnow()
    for event in undelivered:
        if not event.source or event.origin_event_id is None:
            bot_events.ensure_event_identity(event)
            db.session.commit()
        message = _json.dumps(
            {
                "id": event.origin_event_id,
                "source": event.source,
                "sender": bot_events.event_source(),
                "created_at_ms": int(event.created_at.replace(tzinfo=dt.timezone.utc).timestamp() * 1000),
                "type": event.type,
                "telegram_id": event.telegram_id,
                "payload": event.payload,
            }
        )
        try:
            subscribers = redis_client.publish("bot:events", message)
        except Exception as exc:
            logger.info("replay: publish failed for event=%s: %s", event.id, exc)
            continue
        if subscribers == 0:
            event.delivered_at = now
    db.session.commit()


def cleanup_bot_events() -> None:

    now = dt.datetime.utcnow()
    BotEvent.query.filter(
        BotEvent.delivered_at.isnot(None),
        BotEvent.created_at < now - dt.timedelta(days=7),
    ).delete(synchronize_session=False)
    BotEvent.query.filter(
        BotEvent.delivered_at.is_(None),
        BotEvent.created_at < now - dt.timedelta(days=30),
    ).delete(synchronize_session=False)

    from panel_core.models import BotDelivery, NotificationClaim, NotificationLog, ProvisionReceipt

    if inspect(db.session.connection()).has_table(NotificationLog.__tablename__):
        NotificationLog.query.filter(NotificationLog.sent_at < now - dt.timedelta(days=90)).delete(
            synchronize_session=False
        )
    BotDelivery.query.filter(
        BotDelivery.state.in_(("delivered", "suppressed", "permanent")),
        BotDelivery.updated_at < now - dt.timedelta(days=90),
    ).delete(synchronize_session=False)

    NotificationClaim.query.filter(
        NotificationClaim.created_at < now - dt.timedelta(days=90),
    ).delete(synchronize_session=False)

    ProvisionReceipt.query.filter(
        ProvisionReceipt.created_at < now - dt.timedelta(days=90),
    ).delete(synchronize_session=False)

    db.session.commit()
