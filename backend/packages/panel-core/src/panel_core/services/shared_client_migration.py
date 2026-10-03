import hashlib
import json
import time
from collections import defaultdict

from panel_core.extensions import db
from panel_core.models import AccessEntitlement, Client, ClientCredential, NotificationLog, TrafficCounterBaseline
from panel_core.services.runtime_apply import mark_runtime_dirty, runtime_lock

SUPPORTED_PROTOCOLS = {"vless", "vmess", "trojan", "shadowsocks"}


def _row(row):
    return {
        column.name: value.isoformat() if hasattr(value, "isoformat") else value
        for column in row.__table__.columns
        for value in [getattr(row, column.name)]
    }


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _groups():
    groups = defaultdict(list)
    for client in Client.query.filter(Client.telegram_id.isnot(None)).order_by(Client.id).all():
        groups[(client.telegram_id, client.inbound_tag)].append(client)
    return {key: rows for key, rows in groups.items() if len(rows) > 1}


def migration_report():
    result = []
    for (telegram_id, inbound_tag), clients in sorted(_groups().items()):
        sources = AccessEntitlement.query.filter(AccessEntitlement.client_id.in_([row.id for row in clients])).all()
        reasons = []
        if not clients[0].inbound or clients[0].inbound.protocol not in SUPPORTED_PROTOCOLS:
            reasons.append("unsupported_protocol")
        for field in ("preferred_outbound", "manual_disabled", "enable", "disable_reason", "reset_day"):
            if len({getattr(row, field) for row in clients}) > 1:
                reasons.append(f"conflicting_{field}")
        if any(row.expiry_time is None or row.expiry_time < 0 for row in clients):
            reasons.append("damaged_expiry")
        if any(row.up is None or row.down is None or row.up < 0 or row.down < 0 for row in [*clients, *sources]):
            reasons.append("damaged_usage")
        if any(row.telegram_id != telegram_id or row.inbound_tag != inbound_tag for row in sources):
            reasons.append("mismatched_source_target")
        if any((row.up or 0) or (row.down or 0) for row in [*clients, *sources]):
            reasons.append("existing_usage_requires_review")
        if any(row.limit_bytes is None or row.limit_bytes < 0 for row in [*clients, *sources]):
            reasons.append("damaged_limit")
        if len({row.email for row in clients}) != len(clients):
            reasons.append("duplicate_runtime_email")
        if any(not row.enable and not row.manual_disabled for row in clients):
            reasons.append("disabled_legacy_requires_review")
        if any(row.credential_records for row in clients):
            reasons.append("already_has_aliases_requires_review")
        now = int(time.time() * 1000)
        for client in clients:
            own = [row for row in sources if row.client_id == client.id]
            if own:
                active = [
                    row
                    for row in own
                    if row.enabled and not row.revoked and (not row.expires_at_ms or row.expires_at_ms > now)
                ]
                expiry = (
                    0
                    if any(row.expires_at_ms == 0 for row in active)
                    else max((row.expires_at_ms for row in active), default=None)
                )
                if expiry != client.expiry_time:
                    reasons.append("manual_expiry_or_inactive_sources_requires_review")
                if active:
                    owner = max(active, key=lambda row: (row.limit_bytes == 0, row.limit_bytes, row.created_at, row.id))
                    if owner.limit_bytes != client.limit_bytes:
                        reasons.append("manual_quota_requires_review")
            elif client.expiry_time and client.expiry_time <= now:
                reasons.append("expired_legacy_requires_review")
        originals = [_row(row) for row in clients]
        result.append(
            {
                "telegram_id": telegram_id,
                "inbound_tag": inbound_tag,
                "protocol": clients[0].inbound.protocol if clients[0].inbound else None,
                "status": "review" if reasons else "ready",
                "reasons": sorted(set(reasons)),
                "fingerprint": _digest([originals, [_row(row) for row in sources]]),
                "clients": [
                    {
                        "credential_fingerprint": _digest(row.id),
                        "expiry_time": row.expiry_time,
                        "limit_bytes": row.limit_bytes,
                        "up": row.up,
                        "down": row.down,
                        "enable": row.enable,
                        "manual_disabled": row.manual_disabled,
                        "preferred_outbound": row.preferred_outbound,
                    }
                    for row in clients
                ],
                "sources": [
                    {
                        "source_fingerprint": _digest(row.source_id),
                        "tariff_id": row.tariff_id,
                        "expires_at_ms": row.expires_at_ms,
                        "revoked": row.revoked,
                        "enabled": row.enabled,
                        "limit_bytes": row.limit_bytes,
                    }
                    for row in sources
                ],
            }
        )
    return {"groups": result, "mode": "offline_copy", "existing_usage_policy": "requires_review"}


def apply_migration(report):
    with runtime_lock():
        db.session.expire_all()
        current = migration_report()
        if current != report:
            raise ValueError("migration_report_changed")
        ready = {(row["telegram_id"], row["inbound_tag"]) for row in current["groups"] if row["status"] == "ready"}
        merged = 0
        try:
            for key, clients in _groups().items():
                if key not in ready:
                    continue
                canonical = clients[0]
                canonical_before = _row(canonical)
                sources = AccessEntitlement.query.filter(
                    AccessEntitlement.client_id.in_([row.id for row in clients])
                ).all()
                sources_before = [_row(row) for row in sources]
                for client in clients:
                    own = [row for row in sources if row.client_id == client.id]
                    if not own:
                        source = AccessEntitlement(
                            source_id=f"legacy:{client.id}",
                            operation_id=f"legacy:{client.id}",
                            telegram_id=client.telegram_id,
                            tariff_id=client.tariff_id,
                            inbound_tag=client.inbound_tag,
                            client_id=canonical.id,
                            expires_at_ms=client.expiry_time,
                            limit_bytes=client.limit_bytes,
                            up=0,
                            down=0,
                            enabled=True,
                            revoked=False,
                        )
                        db.session.add(source)
                        sources.append(source)
                    for source in own:
                        source.client_id = canonical.id
                    if client is canonical:
                        continue
                    archive = {
                        "client": _row(client),
                        "canonical_before": canonical_before,
                        "sources_before": sources_before,
                    }
                    db.session.add(
                        ClientCredential(
                            id=client.id,
                            client_id=canonical.id,
                            email=client.email,
                            flow=client.flow,
                            wg_address=client.wg_address,
                            original_data=archive,
                        )
                    )
                    NotificationLog.query.filter_by(client_id=client.id).update({"client_id": canonical.id})
                    db.session.delete(client)
                db.session.flush()
                active = [
                    row
                    for row in sources
                    if row.enabled
                    and not row.revoked
                    and (not row.expires_at_ms or row.expires_at_ms > int(time.time() * 1000))
                ]
                owner = max(active, key=lambda row: (row.limit_bytes == 0, row.limit_bytes, row.created_at, row.id))
                canonical.expiry_time = (
                    0 if any(row.expires_at_ms == 0 for row in active) else max(row.expires_at_ms for row in active)
                )
                canonical.limit_bytes, canonical.tariff_id = owner.limit_bytes, owner.tariff_id
                canonical.active_entitlement_source = owner.source_id
                canonical.provisioning_key = f"{canonical.telegram_id}:{canonical.inbound_tag}"
                for baseline in TrafficCounterBaseline.query.filter(
                    TrafficCounterBaseline.entity_type == "user",
                    TrafficCounterBaseline.entity_id.in_([row.id for row in clients]),
                ).all():
                    baseline.traffic_generation = canonical.traffic_generation or ""
                merged += 1
            if merged:
                mark_runtime_dirty()
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return {
            "merged_groups": merged,
            "unresolved_groups": sum(row["status"] == "review" for row in current["groups"]),
        }
