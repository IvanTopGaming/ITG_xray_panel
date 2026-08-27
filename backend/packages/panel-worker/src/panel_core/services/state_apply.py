import datetime
import json

from sqlalchemy import update

from panel_core.extensions import db
from panel_core.models import (
    Admin,
    Balancer,
    Client,
    Inbound,
    NotificationLog,
    Outbound,
    ProvisionReceipt,
    RoutingProfile,
    SystemSetting,
)
from panel_core.services.state_fingerprint import MIRRORED_SETTING_KEYS


def _parse_sent_at(raw):
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _apply_settings(rows):
    for row in rows:
        key = row.get("key")
        if key not in MIRRORED_SETTING_KEYS:
            continue
        existing = db.session.get(SystemSetting, key)
        if existing is None:
            db.session.add(SystemSetting(key=key, value=row.get("value") or ""))
        else:
            existing.value = row.get("value") or ""


def _apply_outbounds(rows):
    disabled = 0
    seen_tags = set()
    if not rows:
        return disabled
    for row in rows:
        seen_tags.add(row["tag"])
        dedicated = bool(row.get("public_ip"))
        existing = Outbound.query.filter_by(tag=row["tag"]).first()
        if existing is None:
            existing = Outbound(tag=row["tag"])
            db.session.add(existing)
        existing.protocol = row.get("protocol") or "freedom"
        existing.settings = row.get("settings") or "{}"
        existing.stream_settings = row.get("stream_settings") or "{}"
        existing.mux = row.get("mux") or "{}"
        existing.send_through = row.get("send_through") or None
        existing.gateway = None if dedicated else (row.get("gateway") or None)
        existing.public_ip = None if dedicated else (row.get("public_ip") or None)
        existing.enable = False if dedicated else bool(row.get("enable", True))
        if dedicated:
            disabled += 1
    Outbound.query.filter(Outbound.tag.notin_(seen_tags)).delete(synchronize_session=False)
    return disabled


def apply_state(hot: dict, cold: dict, *, carry_admin: bool) -> dict:
    for model in (NotificationLog, ProvisionReceipt, Client, Inbound, Balancer, RoutingProfile):
        model.query.delete()
    db.session.flush()

    _apply_settings(cold.get("settings") or [])
    egress_disabled = _apply_outbounds(cold.get("outbounds") or [])

    for row in cold.get("routing_profiles") or []:
        db.session.add(
            RoutingProfile(
                id=row["id"],
                name=row["name"],
                rules=row.get("rules") or "[]",
                enable=bool(row.get("enable", True)),
            )
        )

    for row in cold.get("balancers") or []:
        db.session.add(
            Balancer(
                tag=row["tag"],
                enable=bool(row.get("enable", True)),
                selector=row.get("selector") or "[]",
                strategy=row.get("strategy") or "random",
                fallback_tag=row.get("fallback_tag"),
            )
        )
    db.session.flush()

    inbound_count = client_count = 0
    for row in hot.get("inbounds") or []:
        db.session.add(
            Inbound(
                id=row.get("id"),
                tag=row["tag"],
                port=row["port"],
                protocol=row.get("protocol") or "vless",
                stream_settings=json.dumps(row.get("stream_settings") or {}),
                routing_profile_id=row.get("routing_profile_id"),
                up=row.get("up") or 0,
                down=row.get("down") or 0,
                fallback_address=row.get("fallback_address") or None,
                label=row.get("label") or None,
            )
        )
        inbound_count += 1
    db.session.flush()

    damaged_expiry_ids = []
    for row in hot.get("inbounds") or []:
        for c in row.get("clients") or []:
            expiry_time = c.get("expiry_time")
            if expiry_time is None:
                damaged_expiry_ids.append(c["id"])
            db.session.add(
                Client(
                    id=c["id"],
                    email=c["email"],
                    inbound_tag=c.get("inbound_tag") or row["tag"],
                    limit_bytes=c.get("limit_bytes") or 0,
                    expiry_time=expiry_time if expiry_time is not None else 0,
                    up=c.get("up") or 0,
                    down=c.get("down") or 0,
                    enable=bool(c.get("enable", True)),
                    reset_day=c.get("reset_day") or 0,
                    last_reset_time=c.get("last_reset_time") or 0,
                    last_seen=c.get("last_seen") or 0,
                    source_ips=c.get("source_ips") or "[]",
                    flow=c.get("flow") or None,
                    preferred_outbound=c.get("preferred_outbound") or None,
                    telegram_id=c.get("telegram_id"),
                    tariff_id=c.get("tariff_id"),
                )
            )
            client_count += 1
    db.session.flush()
    if damaged_expiry_ids:
        db.session.execute(update(Client).where(Client.id.in_(damaged_expiry_ids)).values(expiry_time=None))

    for row in cold.get("receipts") or []:
        db.session.add(
            ProvisionReceipt(
                idempotency_key=row["idempotency_key"],
                inbound_tag=row["inbound_tag"],
                telegram_id=row["telegram_id"],
                response_json=row["response_json"],
                materialized=bool(row.get("materialized")),
            )
        )

    for row in cold.get("notification_logs") or []:
        db.session.add(
            NotificationLog(
                telegram_id=row["telegram_id"],
                client_id=row["client_id"],
                kind=row["kind"],
                sent_at=_parse_sent_at(row.get("sent_at")),
            )
        )

    admin_row = cold.get("admin")
    if carry_admin and admin_row:
        Admin.query.delete()
        db.session.flush()
        db.session.add(
            Admin(
                username=admin_row["username"],
                password=admin_row["password"],
                password_changed_at=admin_row.get("password_changed_at") or 0,
            )
        )

    db.session.commit()
    return {
        "inbounds": inbound_count,
        "clients": client_count,
        "outbounds": len(cold.get("outbounds") or []),
        "egress_disabled": egress_disabled,
    }
