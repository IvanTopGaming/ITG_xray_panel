import json
import logging
import secrets
import time
import uuid
from typing import TYPE_CHECKING

from panel_core.extensions import db
from panel_core.models import Client, Inbound, LinkedPanel
from panel_core.services import sub_cache
from panel_core.services.panel_proxy import fetch_panel_snapshot_live
from panel_core.xray.facade import (
    _api_add_user_grpc,
    generate_config_file,
    has_local_xray,
    restart_xray_container,
)
from panel_core.xray.gateway import LocalXrayUnavailable
from panel_core.xray.protocol import inbound_supports_vless_flow

if TYPE_CHECKING:
    from panel_core.models import Tariff, TariffItem

logger = logging.getLogger(__name__)

_GB = 1024**3


def _require_local_xray(what: str) -> None:
    if has_local_xray():
        return
    raise LocalXrayUnavailable(
        f"{what} requires a local Xray instance, which this role does not run. "
        f"Set panel_id on the tariff item so the user is provisioned on a node."
    )


def _sync_after_provision(
    new_clients: list,
    extended_clients_with_state: list,
) -> None:

    all_clients = new_clients + [c for c, _ in extended_clients_with_state]

    for c in all_clients:
        try:
            sub_cache.invalidate_user(c.id)
        except Exception as exc:
            logger.info("sub_cache.invalidate_user failed: %s", exc)

    generate_config_file()

    inbound_tags = {c.inbound_tag for c in all_clients}
    inbounds_by_tag = (
        {ib.tag: ib for ib in Inbound.query.filter(Inbound.tag.in_(inbound_tags)).all()} if inbound_tags else {}
    )

    grpc_adds: list[tuple[str, Client]] = []
    for c in new_clients:
        ib = inbounds_by_tag.get(c.inbound_tag)
        if not ib or ib.protocol not in ("vless", "vmess"):
            restart_xray_container()
            return
        grpc_adds.append((c.inbound_tag, c))

    for c, was_enabled in extended_clients_with_state:
        ib = inbounds_by_tag.get(c.inbound_tag)
        if not ib or ib.protocol not in ("vless", "vmess"):
            restart_xray_container()
            return
        if not was_enabled:
            grpc_adds.append((c.inbound_tag, c))

    for tag, client in grpc_adds:
        if not _api_add_user_grpc(tag, client):
            restart_xray_container()
            return


def _generate_identity(protocol: str) -> str:
    if protocol in ("vless", "vmess"):
        return str(uuid.uuid4())
    return secrets.token_urlsafe(16)


def _generate_email(telegram_id: int, inbound_tag: str) -> str:

    return f"tg{telegram_id}_{inbound_tag}"


def _create_client_for_item(
    *,
    telegram_id: int,
    tariff: "Tariff",
    item: "TariffItem",
    expiry_ms: int,
    limit_bytes: int,
) -> Client:
    _require_local_xray(f"creating a client on local inbound {item.inbound_tag!r} for tariff {tariff.name!r}")

    inbound = Inbound.query.filter_by(tag=item.inbound_tag).first()
    if inbound is None:
        raise ValueError(f"Inbound {item.inbound_tag!r} referenced by tariff item not found")

    identity = _generate_identity(inbound.protocol)
    base_email = _generate_email(telegram_id, item.inbound_tag)
    email = base_email
    for _attempt in range(8):
        if not Client.query.filter_by(inbound_tag=item.inbound_tag, email=email).first():
            break
        email = f"{base_email}_{secrets.token_hex(3)}"
    else:
        raise RuntimeError(
            f"Could not find a unique email for tg={telegram_id} inbound={item.inbound_tag} after 8 attempts"
        )

    client = Client(
        id=identity,
        email=email,
        inbound_tag=item.inbound_tag,
        telegram_id=telegram_id,
        tariff_id=tariff.id,
        limit_bytes=limit_bytes,
        expiry_time=expiry_ms,
        last_reset_time=int(time.time() * 1000),
        up=0,
        down=0,
        enable=True,
        flow="xtls-rprx-vision" if inbound_supports_vless_flow(inbound) else "",
    )
    db.session.add(client)
    return client


def _validate_provision_semantics(
    expiry_ms: int | None,
    period_ms: int | None,
    idempotency_key: str | None,
) -> None:

    if (expiry_ms is None) == (period_ms is None):
        raise ValueError(
            "provision takes exactly one of 'period_ms' (extend by that many milliseconds) "
            "or 'expiry_ms' (set that absolute expiry), never both and never neither"
        )
    if period_ms is not None:
        if not isinstance(period_ms, int) or isinstance(period_ms, bool) or period_ms < 0:
            raise ValueError("'period_ms' must not be negative")
        if not idempotency_key:
            raise ValueError(
                "'idempotency_key' is required alongside 'period_ms': extending is not idempotent on its own, "
                "so a retried request would add the period twice"
            )
    if expiry_ms is not None and (not isinstance(expiry_ms, int) or isinstance(expiry_ms, bool) or expiry_ms < 0):
        raise ValueError("expiry_ms_must_be_nonnegative_integer")
    if expiry_ms is not None and idempotency_key is not None:
        raise ValueError("expiry_ms_must_not_carry_period_idempotency_key")


def _find_receipt_row(idempotency_key: str, inbound_tag: str):

    from panel_core.models import ProvisionReceipt

    return ProvisionReceipt.query.filter_by(idempotency_key=idempotency_key, inbound_tag=inbound_tag).first()


def _find_receipt(idempotency_key: str, inbound_tag: str) -> dict | None:

    prior = _find_receipt_row(idempotency_key, inbound_tag)
    if prior is None:
        return None
    try:
        return json.loads(prior.response_json)
    except (TypeError, ValueError):
        logger.warning(
            "provision receipt %r/%r is unreadable; treating the request as new",
            idempotency_key,
            inbound_tag,
        )
        return None


def _target_expiry_ms(
    client: Client | None,
    *,
    now_ms: int,
    expiry_ms: int | None,
    period_ms: int | None,
) -> int:

    if period_ms is None:
        return expiry_ms

    current = client.expiry_time if client is not None else None
    if current == 0:
        return 0
    return max(now_ms, current or 0) + period_ms


def _preserve_foreign_expiry(current: int | None, requested: int) -> int:
    if current == 0 or requested == 0:
        return 0
    return max(current or 0, requested)


def provision_single_item(
    *,
    telegram_id: int,
    inbound_tag: str,
    limit_bytes: int,
    expiry_ms: int | None = None,
    period_ms: int | None = None,
    tariff_id: int | None = None,
    idempotency_key: str | None = None,
    source_id: str | None = None,
    source_revision: int = 0,
    operation_id: str | None = None,
    account_revision: int = 0,
) -> dict:
    _validate_provision_semantics(expiry_ms, period_ms, idempotency_key)
    if not isinstance(telegram_id, int) or isinstance(telegram_id, bool):
        raise ValueError("telegram_id_must_be_integer")
    if not isinstance(inbound_tag, str) or not inbound_tag:
        raise ValueError("inbound_tag_required")
    for name, value in (
        ("limit_bytes", limit_bytes),
        ("source_revision", source_revision),
        ("account_revision", account_revision),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name}_must_be_nonnegative_integer")
    _require_local_xray(f"provisioning local inbound {inbound_tag!r}")
    from panel_core.services.entitlements import provision

    return provision(
        telegram_id=telegram_id,
        inbound_tag=inbound_tag,
        limit_bytes=limit_bytes,
        expiry_ms=expiry_ms,
        period_ms=period_ms,
        tariff_id=tariff_id,
        idempotency_key=idempotency_key,
        source_id=source_id,
        source_revision=source_revision,
        operation_id=operation_id,
        account_revision=account_revision,
    )


def clear_notification_claims(*, telegram_id: int, tariff_id: int | None) -> None:

    from panel_core.models import NotificationClaim

    NotificationClaim.query.filter(
        NotificationClaim.telegram_id == telegram_id,
        NotificationClaim.tariff_id == int(tariff_id or 0),
    ).delete(synchronize_session=False)


def apply_tariff_for_user(
    telegram_id,
    tariff,
    *,
    source,
    operation_id,
    expiry_ms=None,
    guard=None,
    source_id=None,
    source_revision=0,
    notification=None,
):
    if not operation_id:
        raise ValueError("operation_id_required")
    from panel_core.services.provisioning_operations import apply_grant

    result = apply_grant(
        telegram_id,
        tariff,
        source=source,
        operation_id=operation_id,
        expiry_ms=expiry_ms,
        guard=guard,
        source_id=source_id,
        source_revision=source_revision,
        notification=notification,
    )
    clear_notification_claims(telegram_id=telegram_id, tariff_id=tariff.id)
    db.session.commit()
    sub_cache.invalidate_user_aggregate(telegram_id)
    return result


def revoke_payment_access(
    telegram_id, tariff_id, *, operation_id=None, tariff_snapshot=None, pending_targets=None, guard=None
):
    from panel_core.services.provisioning_operations import revoke_source_operation

    if not operation_id:
        return {
            "disabled_clients": 0,
            "remote_disabled": 0,
            "panel_failures": [{"error": "payment_source_required_for_safe_refund"}],
        }
    result = revoke_source_operation(
        telegram_id, tariff_id, source_id=operation_id, snapshot=tariff_snapshot, guard=guard
    )
    sub_cache.invalidate_user_aggregate(telegram_id)
    return result


def _collect_tariff_holders(tariff: "Tariff", now_ms: int, *, panel_ids=None):

    records = []
    for c in Client.query.filter(Client.telegram_id.isnot(None), Client.tariff_id == tariff.id).all():
        if c.expiry_time is None:
            raise ValueError(f"Missing expiry for tariff client {c.id}")
        active = bool(c.enable) and (c.expiry_time == 0 or c.expiry_time > now_ms)
        records.append((c.telegram_id, None, c.inbound_tag, c.tariff_id, active, c.expiry_time or 0))

    unreachable_ids: set[int] = set()
    child_panel_ids = {it.panel_id for it in tariff.items if it.panel_id is not None}
    child_panel_ids.update(panel_ids or [])
    for pid in child_panel_ids:
        try:
            snap = fetch_panel_snapshot_live(pid)
        except Exception as exc:
            logger.warning("backfill: snapshot fetch failed for panel=%s: %s", pid, exc)
            unreachable_ids.add(pid)
            continue
        for ib in snap.get("inbounds", []):
            tag = ib.get("tag")
            for cl in ib.get("clients", []):
                tg = cl.get("telegram_id")
                if tg is None:
                    continue
                if cl.get("tariff_id") != tariff.id:
                    continue
                exp = cl.get("expiry_time")
                if exp is None:
                    raise ValueError(f"Missing expiry for tariff holder {tg} on panel {pid}")
                active = bool(cl.get("enable")) and (exp == 0 or exp > now_ms)
                records.append((tg, pid, tag, cl.get("tariff_id"), active, exp))

    holders: dict[int, dict] = {}
    for tg, _panel_id, _tag, tid, active, exp in records:
        if tid == tariff.id and active:
            h = holders.get(tg)
            if h is None:
                holders[tg] = {"expiry_ms": exp, "have": set()}
            else:
                prev = h["expiry_ms"]
                h["expiry_ms"] = 0 if (prev == 0 or exp == 0) else max(prev, exp)

    for tg, panel_id, tag, tid, _active, _exp in records:
        if tg in holders and tid == tariff.id:
            holders[tg]["have"].add((panel_id, tag))

    return holders, unreachable_ids


def queue_tariff_backfill(tariff, *, previous_panel_ids=()):
    from panel_core.models import ProvisionOperation
    from panel_core.services.provisioning_operations import snapshot_tariff

    operation_id = f"tariff-rollout:{uuid.uuid4()}"
    operation = ProvisionOperation(
        id=operation_id,
        kind="tariff_backfill",
        telegram_id=0,
        tariff_id=tariff.id,
        source="tariff_rollout",
        source_id=operation_id,
        source_revision=0,
        snapshot={**snapshot_tariff(tariff), "discovery_panel_ids": sorted(set(previous_panel_ids))},
        params={},
    )
    db.session.add(operation)
    return operation


def _backfill_sources(tariff_id, telegram_id, expiry_ms):
    from datetime import timezone

    from panel_core.models import AccessEntitlement, ProvisionOperation, UserTariffAccess

    now_ms = int(time.time() * 1000)
    operations = ProvisionOperation.query.filter_by(tariff_id=tariff_id, telegram_id=telegram_id).all()
    latest = {}
    for operation in operations:
        latest[operation.source_id] = max(latest.get(operation.source_id, 0), operation.source_revision)
    revoked = {
        operation.source_id
        for operation in operations
        if operation.kind == "revoke" and operation.source_revision == latest[operation.source_id]
    }
    sources = {}
    local = {}
    for row in AccessEntitlement.query.filter_by(
        tariff_id=tariff_id, telegram_id=telegram_id, revoked=False, enabled=True
    ).all():
        if row.source_id in revoked or row.source_revision < latest.get(row.source_id, 0):
            continue
        previous = local.get(row.source_id)
        if previous is None or row.source_revision > previous["revision"]:
            local[row.source_id] = {"expiry_ms": row.expires_at_ms, "revision": row.source_revision}
        elif row.source_revision == previous["revision"]:
            previous["expiry_ms"] = (
                0 if 0 in (previous["expiry_ms"], row.expires_at_ms) else max(previous["expiry_ms"], row.expires_at_ms)
            )
    canonical = [
        operation
        for operation in operations
        if operation.kind == "grant"
        and operation.status == "succeeded"
        and operation.source != "tariff_backfill"
        and operation.source_id not in revoked
        and operation.source_revision == latest[operation.source_id]
    ]
    for operation in canonical:
        if operation.source_id in local:
            sources[operation.source_id] = local[operation.source_id]
            continue
        end = (operation.params or {}).get("expiry_ms")
        if end is None:
            known = []
            for state in (operation.target_states or {}).values():
                reply = state.get("reply", {})
                value = reply.get("source_expires_at_ms")
                if (
                    reply.get("source_id") != operation.source_id
                    or reply.get("source_revision") != operation.source_revision
                    or not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                ):
                    raise ValueError(f"source_expiry_requires_review:{operation.source_id}")
                known.append(value)
            if not known:
                raise ValueError(f"source_expiry_requires_review:{operation.source_id}")
            end = 0 if 0 in known else max(known)
        sources[operation.source_id] = {"expiry_ms": end, "revision": operation.source_revision}
    sources.update(local)
    grant = UserTariffAccess.query.filter_by(
        telegram_id=telegram_id, tariff_id=tariff_id, billing="free", provisioning_status="succeeded"
    ).first()
    if grant is not None:
        end = (
            0 if grant.access_until is None else int(grant.access_until.replace(tzinfo=timezone.utc).timestamp() * 1000)
        )
        sources[f"grant:{grant.id}"] = {"expiry_ms": end, "revision": grant.provisioning_revision}
    legacy_id = f"legacy-tariff:{tariff_id}:user:{telegram_id}"
    legacy_history = operations and all(
        operation.source == "tariff_backfill" and operation.source_id == legacy_id and operation.kind == "grant"
        for operation in operations
    )
    if not sources and (not operations or legacy_history):
        sources[legacy_id] = {"expiry_ms": expiry_ms, "revision": latest.get(legacy_id, 0)}
    return {
        source_id: source
        for source_id, source in sources.items()
        if source["expiry_ms"] == 0 or source["expiry_ms"] > now_ms
    }


def run_tariff_backfill(operation):
    import datetime as dt
    import hashlib
    from types import SimpleNamespace
    from sqlalchemy import or_, update
    from panel_core.models import ProvisionOperation
    from panel_core.services.provisioning_operations import frozen_tariff, queue_grant, run_operation

    if operation.status in {"succeeded", "superseded"}:
        return {**(operation.params or {}).get("summary", {}), "status": operation.status, "operation_id": operation.id}
    owner = str(uuid.uuid4())
    moment = dt.datetime.utcnow()
    claim = db.session.execute(
        update(ProvisionOperation)
        .where(
            ProvisionOperation.id == operation.id,
            or_(ProvisionOperation.processing_owner.is_(None), ProvisionOperation.processing_expires_at <= moment),
        )
        .values(processing_owner=owner, processing_expires_at=moment + dt.timedelta(minutes=2))
    )
    db.session.commit()
    if not claim.rowcount:
        return {"status": "pending", "operation_id": operation.id, "provision_failures": 0}

    def renew(**values):
        moment = dt.datetime.utcnow()
        changed = db.session.execute(
            update(ProvisionOperation)
            .where(
                ProvisionOperation.id == operation.id,
                ProvisionOperation.processing_owner == owner,
                ProvisionOperation.processing_expires_at > moment,
            )
            .values(processing_expires_at=moment + dt.timedelta(minutes=2), **values)
        )
        db.session.commit()
        if not changed.rowcount:
            raise RuntimeError("Tariff rollout lease lost")

    summary = {
        "holders": 0,
        "created_local": 0,
        "created_remote": 0,
        "skipped_existing": 0,
        "provision_failures": 0,
        "panels_unreachable": [],
        "operation_id": operation.id,
    }
    try:
        tariff = frozen_tariff(operation)
        holders, unreachable = _collect_tariff_holders(
            tariff, int(time.time() * 1000), panel_ids=operation.snapshot.get("discovery_panel_ids", [])
        )
        summary["holders"] = len(holders)
        panel_names = dict(db.session.query(LinkedPanel.id, LinkedPanel.name).all())
        summary["panels_unreachable"] = sorted(panel_names.get(pid, str(pid)) for pid in unreachable)
        children = dict(operation.target_states or {})
        for tg, info in holders.items():
            missing = [
                item
                for item in tariff.items
                if item.panel_id not in unreachable and (item.panel_id, item.inbound_tag) not in info["have"]
            ]
            summary["skipped_existing"] += sum(
                (item.panel_id, item.inbound_tag) in info["have"] for item in tariff.items
            )
            if not missing:
                continue
            sources = _backfill_sources(tariff.id, tg, info["expiry_ms"])
            if not sources:
                raise ValueError(f"active_holder_source_requires_review:{tariff.id}:{tg}")
            for source_id, source in sources.items():
                child_key = f"{tg}:{hashlib.sha256(source_id.encode()).hexdigest()[:16]}"
                if child_key not in children:
                    child_id = f"{operation.id}:{child_key}"
                    child = db.session.get(ProvisionOperation, child_id) or queue_grant(
                        tg,
                        SimpleNamespace(id=tariff.id, name=tariff.name, period_days=tariff.period_days, items=missing),
                        source="tariff_backfill",
                        operation_id=child_id,
                        source_id=source_id,
                        source_revision=source["revision"],
                        expiry_ms=source["expiry_ms"],
                    )
                    children[child_key] = {"operation_id": child.id}
                renew(target_states=dict(children))
        for child_info in children.values():
            renew()
            child = db.session.get(ProvisionOperation, child_info["operation_id"])
            if child.status == "succeeded":
                result = {"status": "succeeded"}
            else:
                result = run_operation(child)
            if result.get("status") not in {"succeeded", "superseded"}:
                summary["provision_failures"] += 1
            else:
                summary["created_local"] += sum(item["panel_id"] is None for item in child.snapshot["items"])
                summary["created_remote"] += sum(item["panel_id"] is not None for item in child.snapshot["items"])
        summary["status"] = "pending" if unreachable or summary["provision_failures"] else "succeeded"
        renew(
            status=summary["status"],
            params={"summary": summary},
            last_checked_at=dt.datetime.utcnow(),
            last_error="tariff_rollout_incomplete" if summary["status"] == "pending" else None,
        )
        return summary
    except Exception:
        db.session.rollback()
        logger.exception("tariff rollout failed: operation_id=%s", operation.id)
        db.session.execute(
            update(ProvisionOperation)
            .where(ProvisionOperation.id == operation.id, ProvisionOperation.processing_owner == owner)
            .values(status="pending", last_checked_at=dt.datetime.utcnow(), last_error="tariff_rollout_failed")
        )
        db.session.commit()
        raise
    finally:
        db.session.execute(
            update(ProvisionOperation)
            .where(ProvisionOperation.id == operation.id, ProvisionOperation.processing_owner == owner)
            .values(processing_owner=None, processing_expires_at=None)
        )
        db.session.commit()


def backfill_tariff(tariff: "Tariff") -> dict:
    operation = queue_tariff_backfill(tariff)
    db.session.commit()
    return run_tariff_backfill(operation)
