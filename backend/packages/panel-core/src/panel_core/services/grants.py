import datetime as dt

from sqlalchemy import update

from panel_core.extensions import db
from panel_core.models import Client, LinkedPanel, ProvisionOperation, TelegramUser, UserTariffAccess
from panel_core.services.provisioning_operations import now, queue_grant, queue_operation, run_operation
from panel_core.xray.facade import has_local_xray


def expiry_ms(value):
    if value is None:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return int(value.timestamp() * 1000)


def save_grant(telegram_id, tariff, *, billing, access_until, note=None, silent=False):
    from panel_core.services import bot_events
    from panel_core.services.tariff_targets import validate_tariff_targets

    if billing == "free" and (not tariff.enabled or not tariff.items):
        raise ValueError("tariff_has_no_deliverable_items")
    if billing == "free":
        validate_tariff_targets(tariff)
    user = db.session.get(TelegramUser, telegram_id)
    if user is not None and user.blocked:
        raise ValueError("account_blocked")
    if user is None:
        user = TelegramUser(telegram_id=telegram_id, language="ru")
        db.session.add(user)
    grant = UserTariffAccess.query.filter_by(telegram_id=telegram_id, tariff_id=tariff.id).first()
    if grant is not None and billing == "paid" and grant.billing == "free" and grant.provisioning_status != "revoked":
        raise ValueError("revoke_issued_access_before_changing_to_paid_offer")
    if (
        grant is not None
        and grant.provisioning_status == "pending"
        and grant.billing == billing
        and grant.access_until == access_until
    ):
        operation = db.session.get(ProvisionOperation, f"grant:{grant.id}:{grant.provisioning_revision}")
        if operation is not None:
            grant.note = note
            return grant, run_operation(operation)
    if grant is None:
        grant = UserTariffAccess(telegram_id=telegram_id, tariff_id=tariff.id, billing=billing)
        db.session.add(grant)
    grant.billing, grant.note = billing, note
    grant.legacy_migration_version = 1
    grant.access_until = access_until if billing == "free" else None
    grant.next_renewal_at = (
        now() + dt.timedelta(days=tariff.period_days)
        if billing == "free" and any(item.traffic_gb for item in tariff.items)
        else None
    )
    if billing == "paid":
        grant.provisioning_status = "succeeded"
        event = (
            None
            if silent
            else bot_events.enqueue(
                "access_offered", telegram_id, {"tariff_name": tariff.name, "lang": user.language or "ru"}
            )
        )
        db.session.commit()
        if event is not None:
            bot_events.publish_stored(event)
        return grant, None
    db.session.flush()
    revision = db.session.execute(
        update(UserTariffAccess)
        .where(UserTariffAccess.id == grant.id)
        .values(provisioning_revision=UserTariffAccess.provisioning_revision + 1, provisioning_status="pending")
        .returning(UserTariffAccess.provisioning_revision)
    ).scalar_one()
    notification = (
        None
        if silent
        else {"type": "access_granted", "payload": {"tariff_name": tariff.name, "lang": user.language or "ru"}}
    )
    operation = queue_grant(
        telegram_id,
        tariff,
        source="admin_grant",
        operation_id=f"grant:{grant.id}:{revision}",
        source_id=f"grant:{grant.id}",
        source_revision=revision,
        expiry_ms=expiry_ms(access_until),
        notification=notification,
    )
    return grant, run_operation(operation)


def _target_counts(operation):
    counts = {"disabled_clients": 0, "remote_disabled": 0, "re_enabled": 0, "remote_re_enabled": 0}
    for item in operation.snapshot["items"]:
        key = f"{item.get('panel_id') or 0}:{item.get('inbound_tag') or ''}"
        state = (operation.target_states or {}).get(key, {})
        if state.get("status") != "succeeded":
            continue
        reply = state.get("reply", {})
        remote = item.get("panel_id") is not None
        counts["remote_disabled" if remote else "disabled_clients"] += reply.get("disabled_clients", 0)
        counts["remote_re_enabled" if remote else "re_enabled"] += reply.get("re_enabled", 0)
    return counts


def set_account_block(telegram_id, blocked):
    user = db.session.get(TelegramUser, telegram_id)
    if user is None:
        raise ValueError("user_not_found")
    revision = db.session.execute(
        update(TelegramUser)
        .where(TelegramUser.telegram_id == telegram_id)
        .values(blocked=blocked, access_revision=TelegramUser.access_revision + 1)
        .returning(TelegramUser.access_revision)
    ).scalar_one()
    items = [{"panel_id": panel.id} for panel in LinkedPanel.query.all()]
    if has_local_xray():
        items.append({"panel_id": None})
    operation = queue_operation(
        operation_id=f"account:{telegram_id}:{revision}",
        telegram_id=telegram_id,
        tariff_id=None,
        source="account",
        source_id=f"account:{telegram_id}",
        source_revision=revision,
        kind="account",
        snapshot={"items": items},
        params={
            "revision": revision,
            "blocked": blocked,
            "notification": {"type": "user_blocked" if blocked else "user_unblocked", "payload": {}},
        },
    )
    result = run_operation(operation)
    result.update(
        ok=not result["panel_failures"],
        telegram_id=telegram_id,
        cancelled_grants=0,
        **_target_counts(operation),
    )
    return result


def revoke_tariff(telegram_id, tariff_id):
    targets = {}
    for operation in ProvisionOperation.query.filter_by(telegram_id=telegram_id, tariff_id=tariff_id).all():
        for item in operation.snapshot.get("items", []):
            if item.get("inbound_tag"):
                targets[(item.get("panel_id"), item["inbound_tag"])] = {
                    "panel_id": item.get("panel_id"),
                    "inbound_tag": item["inbound_tag"],
                }
    for client in Client.query.filter_by(telegram_id=telegram_id, tariff_id=tariff_id).all():
        targets[(None, client.inbound_tag)] = {"panel_id": None, "inbound_tag": client.inbound_tag}
    from panel_core.models import TariffItem

    for item in TariffItem.query.filter_by(tariff_id=tariff_id).all():
        targets[(item.panel_id, item.inbound_tag)] = {"panel_id": item.panel_id, "inbound_tag": item.inbound_tag}
    grant = UserTariffAccess.query.filter_by(telegram_id=telegram_id, tariff_id=tariff_id).first()
    if grant is not None:
        grant.provisioning_status = "revoking"
    source_id = f"tariff:{telegram_id}:{tariff_id}"
    current = (
        ProvisionOperation.query.filter_by(source_id=source_id, kind="revoke", status="pending")
        .order_by(ProvisionOperation.source_revision.desc())
        .first()
    )
    if current is None:
        revoked_sources = {}
        for previous in ProvisionOperation.query.filter_by(
            telegram_id=telegram_id, tariff_id=tariff_id, kind="grant"
        ).all():
            revoked_sources[previous.source_id] = max(
                revoked_sources.get(previous.source_id, 0), previous.source_revision + 1
            )
            previous.status = "superseded"
        if grant is not None:
            grant.provisioning_revision += 1
            revoked_sources[f"grant:{grant.id}"] = grant.provisioning_revision
        latest = (
            ProvisionOperation.query.filter_by(source_id=source_id)
            .order_by(ProvisionOperation.source_revision.desc())
            .first()
        )
        revision = (latest.source_revision if latest else 0) + 1
        current = queue_operation(
            operation_id=f"revoke-tariff:{telegram_id}:{tariff_id}:{revision}",
            telegram_id=telegram_id,
            tariff_id=tariff_id,
            source="tariff_revoke",
            source_id=source_id,
            source_revision=revision,
            kind="revoke",
            snapshot={"items": list(targets.values())},
            params={"revoked_sources": revoked_sources},
        )
    result = run_operation(current)
    if grant is not None and not result["panel_failures"]:
        grant.provisioning_status = "revoked"
        grant.access_until = now()
        grant.next_renewal_at = None
        db.session.commit()
    result.update(
        ok=not result["panel_failures"],
        telegram_id=telegram_id,
        tariff_id=tariff_id,
        **_target_counts(current),
        revoked_grants=int(grant is not None and not result["panel_failures"]),
    )
    return result
