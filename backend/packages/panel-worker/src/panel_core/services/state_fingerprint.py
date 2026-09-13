import hashlib
import json

from panel_core.extensions import db
from panel_core.models import (
    Balancer,
    AccessEntitlement,
    AccountAccessState,
    BotEvent,
    NotificationLog,
    Outbound,
    ProvisionReceipt,
    RoutingProfile,
    SystemSetting,
)

MIRRORED_SETTING_KEYS = ("xray_log_level", "geoip_url", "geosite_url")


def _config_digest_input():
    outbounds = [
        [
            o.tag,
            o.protocol,
            bool(o.enable),
            o.settings,
            o.stream_settings,
            o.mux,
            o.send_through,
            o.public_ip,
            o.gateway,
        ]
        for o in Outbound.query.order_by(Outbound.tag).all()
    ]
    profiles = [[p.id, p.name, p.rules, bool(p.enable)] for p in RoutingProfile.query.order_by(RoutingProfile.id).all()]
    balancers = [
        [b.tag, bool(b.enable), b.selector, b.strategy, b.fallback_tag]
        for b in Balancer.query.order_by(Balancer.tag).all()
    ]
    settings = [
        [row.key, row.value]
        for row in SystemSetting.query.filter(SystemSetting.key.in_(MIRRORED_SETTING_KEYS))
        .order_by(SystemSetting.key)
        .all()
    ]
    return [outbounds, profiles, balancers, settings]


def _append_only_counters():
    receipts_total = db.session.query(db.func.count(ProvisionReceipt.id)).scalar() or 0
    receipts_max = db.session.query(db.func.max(ProvisionReceipt.id)).scalar() or 0
    receipts_unmaterialized = (
        db.session.query(db.func.count(ProvisionReceipt.id)).filter(ProvisionReceipt.materialized.is_(False)).scalar()
        or 0
    )
    logs_total = db.session.query(db.func.count(NotificationLog.id)).scalar() or 0
    logs_max = db.session.query(db.func.max(NotificationLog.id)).scalar() or 0
    return [receipts_total, receipts_max, receipts_unmaterialized, logs_total, logs_max]


def compute_fingerprint() -> str:
    entitlements = [
        [getattr(row, column.name) for column in AccessEntitlement.__table__.columns]
        for row in AccessEntitlement.query.order_by(AccessEntitlement.id).all()
    ]
    accounts = [
        [row.telegram_id, row.revision, row.blocked]
        for row in AccountAccessState.query.order_by(AccountAccessState.telegram_id).all()
    ]
    events = [
        [event.source, event.origin_event_id, event.type, event.telegram_id, event.payload]
        for event in BotEvent.query.filter(BotEvent.delivered_at.is_(None)).order_by(BotEvent.id).all()
    ]
    material = json.dumps(
        [_config_digest_input(), _append_only_counters(), events, entitlements, accounts],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()
