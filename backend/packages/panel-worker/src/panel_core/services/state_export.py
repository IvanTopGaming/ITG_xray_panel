import json
import os

from panel_core.models import (
    Admin,
    Balancer,
    Inbound,
    NotificationLog,
    Outbound,
    ProvisionReceipt,
    RoutingProfile,
    SystemSetting,
)
from panel_core.services.state_fingerprint import MIRRORED_SETTING_KEYS

MIRROR_EXCLUDED_COLUMNS = {
    "Client": frozenset({"device_limit"}),
    "Inbound": frozenset({"device_limit"}),
    "Outbound": frozenset({"id"}),
    "RoutingProfile": frozenset(),
    "Balancer": frozenset({"id"}),
    "ProvisionReceipt": frozenset({"id", "created_at"}),
    "NotificationLog": frozenset({"id"}),
}


def _stream_settings(raw):
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _client_row(c):
    return {
        "id": c.id,
        "email": c.email,
        "inbound_tag": c.inbound_tag,
        "enable": bool(c.enable),
        "up": c.up or 0,
        "down": c.down or 0,
        "limit_bytes": c.limit_bytes or 0,
        "expiry_time": c.expiry_time,
        "reset_day": c.reset_day or 0,
        "last_reset_time": c.last_reset_time or 0,
        "last_seen": c.last_seen if c.last_seen else None,
        "source_ips": c.source_ips or "[]",
        "flow": c.flow or "",
        "preferred_outbound": c.preferred_outbound or "",
        "telegram_id": c.telegram_id,
        "tariff_id": c.tariff_id,
    }


def _identity() -> dict:
    return {
        "panel_domain": (os.environ.get("PANEL_DOMAIN") or "").strip(),
        "proxy_domain": (os.environ.get("PROXY_DOMAIN") or "").strip(),
        "secret_path": (os.environ.get("PANEL_SECRET_PATH") or "").strip(),
    }


def export_hot_state() -> dict:
    inbounds = []
    for ib in Inbound.query.order_by(Inbound.id).all():
        inbounds.append(
            {
                "id": ib.id,
                "tag": ib.tag,
                "port": ib.port,
                "protocol": ib.protocol,
                "label": ib.label or "",
                "stream_settings": _stream_settings(ib.stream_settings),
                "up": ib.up or 0,
                "down": ib.down or 0,
                "fallback_address": ib.fallback_address or "",
                "routing_profile_id": ib.routing_profile_id,
                "clients": [_client_row(c) for c in ib.clients],
            }
        )
    return {"inbounds": inbounds}


def export_cold_state() -> dict:
    admin = Admin.query.order_by(Admin.id).first()
    return {
        "outbounds": [
            {
                "tag": o.tag,
                "protocol": o.protocol,
                "enable": bool(o.enable),
                "settings": o.settings,
                "stream_settings": o.stream_settings,
                "mux": o.mux,
                "send_through": o.send_through,
                "public_ip": o.public_ip,
                "gateway": o.gateway,
            }
            for o in Outbound.query.order_by(Outbound.tag).all()
        ],
        "routing_profiles": [
            {"id": p.id, "name": p.name, "rules": p.rules, "enable": bool(p.enable)}
            for p in RoutingProfile.query.order_by(RoutingProfile.id).all()
        ],
        "balancers": [
            {
                "tag": b.tag,
                "enable": bool(b.enable),
                "selector": b.selector,
                "strategy": b.strategy,
                "fallback_tag": b.fallback_tag,
            }
            for b in Balancer.query.order_by(Balancer.tag).all()
        ],
        "settings": [
            {"key": row.key, "value": row.value}
            for row in SystemSetting.query.filter(SystemSetting.key.in_(MIRRORED_SETTING_KEYS))
            .order_by(SystemSetting.key)
            .all()
        ],
        "receipts": [
            {
                "idempotency_key": r.idempotency_key,
                "inbound_tag": r.inbound_tag,
                "telegram_id": r.telegram_id,
                "response_json": r.response_json,
                "materialized": bool(r.materialized),
            }
            for r in ProvisionReceipt.query.order_by(ProvisionReceipt.id).all()
        ],
        "notification_logs": [
            {
                "telegram_id": n.telegram_id,
                "client_id": n.client_id,
                "kind": n.kind,
                "sent_at": n.sent_at.isoformat() if n.sent_at else None,
            }
            for n in NotificationLog.query.order_by(NotificationLog.id).all()
        ],
        "admin": (
            {
                "username": admin.username,
                "password": admin.password,
                "password_changed_at": admin.password_changed_at or 0,
            }
            if admin
            else None
        ),
        "identity": _identity(),
    }
