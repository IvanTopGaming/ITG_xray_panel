import datetime as dt
import logging
import uuid
from types import SimpleNamespace

from sqlalchemy import update, or_, exists
from sqlalchemy.exc import IntegrityError

from panel_core.extensions import db
from panel_core.models import LinkedPanel, ProvisionOperation, TelegramUser, UserTariffAccess
from panel_core.services import bot_events
from panel_core.services.expiry import nearest_expiry
from panel_core.services.panel_proxy import FederationClient, proxy_provision, RemotePanelError
from panel_core.xray.facade import has_local_xray

logger = logging.getLogger(__name__)


def now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def snapshot_tariff(tariff):
    return {
        "name": tariff.name,
        "period_days": tariff.period_days,
        "items": [
            {"panel_id": item.panel_id, "inbound_tag": item.inbound_tag, "traffic_gb": item.traffic_gb}
            for item in tariff.items
        ],
    }


def frozen_tariff(operation):
    return SimpleNamespace(
        id=operation.tariff_id,
        name=operation.snapshot["name"],
        period_days=operation.snapshot["period_days"],
        items=[SimpleNamespace(**item) for item in operation.snapshot["items"]],
    )


def _key(item):
    return f"{item.get('panel_id') or 0}:{item.get('inbound_tag') or ''}"


def _remote(panel_id, path, payload):
    panel = db.session.get(LinkedPanel, panel_id)
    if panel is None or not panel.enable:
        raise ValueError("target_panel_missing_or_disabled")
    return FederationClient(panel.url, panel.federation_token)._call_reporting("post", path, json=payload, timeout=30)


def queue_operation(
    *, operation_id, telegram_id, tariff_id, source, source_id, source_revision, snapshot, params, kind="grant"
):
    existing = db.session.get(ProvisionOperation, operation_id)
    expected = {
        "telegram_id": telegram_id,
        "tariff_id": tariff_id,
        "source": source,
        "source_id": source_id,
        "source_revision": source_revision,
        "snapshot": snapshot,
        "params": params,
        "kind": kind,
    }
    if existing is not None:
        if any(getattr(existing, key) != value for key, value in expected.items()):
            raise ValueError("provision_operation_payload_mismatch")
        return existing
    row = ProvisionOperation(id=operation_id, **expected)
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        row = db.session.get(ProvisionOperation, operation_id)
        if row is None or any(getattr(row, key) != value for key, value in expected.items()):
            raise ValueError("provision_operation_payload_mismatch")
    return row


def queue_grant(
    telegram_id, tariff, *, source, operation_id, expiry_ms=None, source_id=None, source_revision=0, notification=None
):
    snapshot = snapshot_tariff(tariff)
    if not snapshot["items"]:
        raise ValueError("tariff_has_no_deliverable_items")
    if any(item["panel_id"] is None for item in snapshot["items"]) and not has_local_xray():
        from panel_core.xray.gateway import LocalXrayUnavailable

        tags = ", ".join(item["inbound_tag"] for item in snapshot["items"] if item["panel_id"] is None)
        raise LocalXrayUnavailable(
            f"Tariff {tariff.name!r}: inbound {tags} requires panel_id; this role runs no local Xray"
        )
    params = {"expiry_ms": expiry_ms, "notification": notification}
    return queue_operation(
        operation_id=operation_id,
        telegram_id=telegram_id,
        tariff_id=tariff.id,
        source=source,
        source_id=source_id or operation_id,
        source_revision=source_revision,
        snapshot=snapshot,
        params=params,
    )


def _guard(operation, guard):
    if guard is not None:
        guard()
    db.session.refresh(operation)
    if operation.status == "superseded":
        raise ValueError("provision_operation_superseded")
    latest = ProvisionOperation.query.filter(
        ProvisionOperation.source_id == operation.source_id,
        ProvisionOperation.source_revision > operation.source_revision,
    ).first()
    if latest is not None:
        raise ValueError("provision_operation_superseded")
    user = db.session.get(TelegramUser, operation.telegram_id, populate_existing=True)
    if operation.kind == "grant" and user is not None and user.blocked:
        raise ValueError("account_blocked")
    return user.access_revision if user is not None else 0


def _complete(operation, result, owner, version):
    from sqlalchemy.orm import aliased

    newer = aliased(ProvisionOperation)
    conditions = [
        ProvisionOperation.id == operation.id,
        ProvisionOperation.processing_owner == owner,
        ProvisionOperation.processing_version == version,
        ProvisionOperation.processing_expires_at > now(),
        ~exists().where(newer.source_id == operation.source_id, newer.source_revision > operation.source_revision),
    ]
    if operation.kind == "grant":
        conditions.append(
            ~exists().where(TelegramUser.telegram_id == operation.telegram_id, TelegramUser.blocked.is_(True))
        )
    completed = db.session.execute(
        update(ProvisionOperation)
        .where(*conditions, ProvisionOperation.status != "succeeded")
        .values(status="succeeded", last_error=None)
    )
    event = None
    if (
        not completed.rowcount
        and db.session.get(ProvisionOperation, operation.id, populate_existing=True).status != "succeeded"
    ):
        db.session.rollback()
        raise RuntimeError("provision_operation_completion_superseded")
    if completed.rowcount:
        if operation.source == "trial":
            user = db.session.get(TelegramUser, operation.telegram_id)
            if user is not None:
                user.trial_used_at = user.trial_used_at or now()
        if operation.source.startswith("admin_grant") or operation.source == "grant_backfill":
            grant = UserTariffAccess.query.filter_by(
                telegram_id=operation.telegram_id, tariff_id=operation.tariff_id
            ).first()
            if grant is not None and grant.provisioning_revision == operation.source_revision:
                grant.provisioning_status = "succeeded"
        notification = operation.params.get("notification")
        if notification:
            payload = {**notification.get("payload", {}), "expires_at_ms": result.get("expires_at_ms")}
            event = bot_events.enqueue(notification["type"], operation.telegram_id, payload)
    db.session.commit()
    if event is not None:
        bot_events.publish_stored(event)


def run_operation(operation, *, guard=None):
    if operation.kind == "tariff_backfill":
        from panel_core.services.provisioning import run_tariff_backfill

        return run_tariff_backfill(operation)
    owner = str(uuid.uuid4())
    claimed = db.session.execute(
        update(ProvisionOperation)
        .where(
            ProvisionOperation.id == operation.id,
            ProvisionOperation.status != "superseded",
            or_(ProvisionOperation.processing_owner.is_(None), ProvisionOperation.processing_expires_at <= now()),
        )
        .values(
            processing_owner=owner,
            processing_version=ProvisionOperation.processing_version + 1,
            processing_expires_at=now() + dt.timedelta(seconds=120),
        )
        .returning(ProvisionOperation.processing_version)
    ).scalar_one_or_none()
    db.session.commit()
    if claimed is None:
        return {
            "status": operation.status,
            "panel_failures": [{"error": "provision_operation_busy_or_superseded"}],
            "operation_id": operation.id,
        }
    try:
        return _run_claimed(operation, owner, claimed, guard=guard)
    finally:
        db.session.rollback()
        db.session.execute(
            update(ProvisionOperation)
            .where(
                ProvisionOperation.id == operation.id,
                ProvisionOperation.processing_owner == owner,
                ProvisionOperation.processing_version == claimed,
            )
            .values(processing_owner=None, processing_expires_at=None)
        )
        db.session.commit()


def _fence(operation, owner, version, guard):
    revision = _guard(operation, guard)
    updated = db.session.execute(
        update(ProvisionOperation)
        .where(
            ProvisionOperation.id == operation.id,
            ProvisionOperation.processing_owner == owner,
            ProvisionOperation.processing_version == version,
            ProvisionOperation.processing_expires_at > now(),
        )
        .values(processing_expires_at=now() + dt.timedelta(seconds=120))
    )
    if not updated.rowcount:
        db.session.rollback()
        raise RuntimeError("provision_operation_lease_lost")
    db.session.commit()
    return revision


def _run_claimed(operation, owner, version, *, guard=None):
    from panel_core.services.entitlements import apply_account_state, provision, revoke_source

    operation.last_checked_at = now()
    db.session.commit()
    states = dict(operation.target_states or {})
    failures, expiries, clients = [], [], []
    for item in operation.snapshot["items"]:
        key = _key(item)
        try:
            account_revision = _fence(operation, owner, version, guard)
            if operation.kind == "account":
                if states.get(key, {}).get("status") == "succeeded":
                    continue
                payload = {
                    "telegram_id": operation.telegram_id,
                    "revision": operation.params["revision"],
                    "blocked": operation.params["blocked"],
                }
                result = (
                    apply_account_state(**payload)
                    if item["panel_id"] is None
                    else _remote(item["panel_id"], "/api/federation/account-access", payload)
                )
            elif operation.kind == "revoke":
                payload = {
                    "telegram_id": operation.telegram_id,
                    "tariff_id": operation.tariff_id,
                    "inbound_tag": item["inbound_tag"],
                    "source_id": operation.source_id,
                    "source_revision": operation.source_revision,
                    "operation_id": operation.id,
                    "revoked_sources": operation.params.get("revoked_sources"),
                }
                result = (
                    revoke_source(**payload)
                    if item["panel_id"] is None
                    else _remote(item["panel_id"], "/api/federation/entitlements/revoke", payload)
                )
            elif operation.kind == "reset":
                if states.get(key, {}).get("status") == "succeeded":
                    continue
                from panel_core.services.entitlements import reset_source_cycle

                payload = {
                    "telegram_id": operation.telegram_id,
                    "tariff_id": operation.tariff_id,
                    "inbound_tag": item["inbound_tag"],
                    "source_id": operation.source_id,
                    "source_revision": operation.source_revision,
                    "operation_id": operation.id,
                }
                result = (
                    reset_source_cycle(**payload)
                    if item["panel_id"] is None
                    else _remote(item["panel_id"], "/api/federation/entitlements/reset-cycle", payload)
                )
            else:
                payload = {
                    "limit_bytes": item["traffic_gb"] * 1024**3,
                    "tariff_id": operation.tariff_id,
                    "source_id": operation.source_id,
                    "source_revision": operation.source_revision,
                    "operation_id": operation.id,
                    "account_revision": account_revision,
                }
                if operation.params.get("expiry_ms") is None:
                    payload.update(period_ms=operation.snapshot["period_days"] * 86400000, idempotency_key=operation.id)
                else:
                    payload["expiry_ms"] = operation.params["expiry_ms"]
                if item["panel_id"] is None:
                    result = provision(telegram_id=operation.telegram_id, inbound_tag=item["inbound_tag"], **payload)
                else:
                    result = proxy_provision(item["panel_id"], operation.telegram_id, item["inbound_tag"], payload)
                if result.get("expires_at_ms") is None:
                    raise ValueError("provision_reply_missing_expiry")
                expiries.append(result["expires_at_ms"])
                if result.get("client"):
                    clients.append(result["client"])
            states[key] = {"status": "succeeded", "reply": result}
        except Exception as exc:
            db.session.rollback()
            if str(exc) == "provision_operation_superseded":
                db.session.execute(
                    update(ProvisionOperation)
                    .where(
                        ProvisionOperation.id == operation.id,
                        ProvisionOperation.processing_owner == owner,
                        ProvisionOperation.processing_version == version,
                    )
                    .values(status="superseded", last_error=str(exc))
                )
                db.session.commit()
                return {"status": "superseded", "panel_failures": [{"error": str(exc)}], "operation_id": operation.id}
            if str(exc) == "provision_operation_lease_lost":
                raise
            message = str(exc)
            error = {"panel_id": item["panel_id"], "inbound_tag": item.get("inbound_tag")}
            if isinstance(exc, RemotePanelError):
                panel = db.session.get(LinkedPanel, item["panel_id"]) if item["panel_id"] is not None else None
                if panel is not None:
                    error["panel_name"] = panel.name
                    message = f"Panel '{panel.name}': {message}"
                error["status_code"] = exc.status_code
                if exc.status_code in (401, 403):
                    message += ". Issue a fresh link token on the node and relink the panel."
            error["error"] = message
            states[key] = {"status": "pending", **error}
            failures.append(error)
        updated = db.session.execute(
            update(ProvisionOperation)
            .where(
                ProvisionOperation.id == operation.id,
                ProvisionOperation.processing_owner == owner,
                ProvisionOperation.processing_version == version,
                ProvisionOperation.processing_expires_at > now(),
            )
            .values(
                target_states=dict(states),
                last_error=failures[-1]["error"] if failures else None,
                status="pending" if failures else operation.status,
            )
        )
        if not updated.rowcount:
            db.session.rollback()
            raise RuntimeError("provision_operation_lease_lost")
        db.session.commit()
    result = {
        "clients": clients,
        "expires_at_ms": nearest_expiry(expiries, fallback=operation.params.get("expiry_ms")),
        "source": operation.source,
        "panel_failures": failures,
        "operation_id": operation.id,
        "status": "pending" if failures else "succeeded",
    }
    if not failures:
        _fence(operation, owner, version, guard)
        _complete(operation, result, owner, version)
    return result


def apply_grant(*args, guard=None, **kwargs):
    operation = queue_grant(*args, **kwargs)
    result = run_operation(operation, guard=guard)
    if result["panel_failures"]:
        raise RuntimeError("provisioning_pending: " + str(result["panel_failures"]))
    return result


def source_targets(source_id, snapshot=None):
    targets = {}
    for item in (snapshot or {}).get("items", []):
        targets[_key(item)] = dict(item)
    for operation in ProvisionOperation.query.filter_by(source_id=source_id).all():
        for item in operation.snapshot.get("items", []):
            targets[_key(item)] = dict(item)
    return list(targets.values())


def revoke_source_operation(telegram_id, tariff_id, *, source_id, snapshot=None, source_revision=1, guard=None):
    targets = source_targets(source_id, snapshot)
    if not targets:
        return {
            "disabled_clients": 0,
            "remote_disabled": 0,
            "panel_failures": [{"error": "entitlement_targets_require_review"}],
        }
    operation = queue_operation(
        operation_id=f"revoke:{source_id}:{source_revision}",
        telegram_id=telegram_id,
        tariff_id=tariff_id,
        source="yookassa" if source_id.startswith("pay:") else "revoke",
        source_id=source_id,
        source_revision=source_revision,
        snapshot={"items": targets},
        params={},
        kind="revoke",
    )
    result = run_operation(operation, guard=guard)
    result.update(
        disabled_clients=sum(
            state.get("reply", {}).get("disabled_clients", 0) for state in operation.target_states.values()
        ),
        remote_disabled=0,
    )
    return result


def retry_pending_operations():
    rows = (
        ProvisionOperation.query.filter(ProvisionOperation.status == "pending", ProvisionOperation.source != "yookassa")
        .order_by(ProvisionOperation.last_checked_at.asc().nullsfirst(), ProvisionOperation.id)
        .limit(200)
        .all()
    )
    for operation in rows:
        try:
            run_operation(operation)
        except Exception:
            db.session.rollback()
            logger.exception("Provision operation recovery failed id=%s", operation.id)
