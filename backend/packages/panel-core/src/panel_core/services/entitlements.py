import json
import datetime as dt
import time
import uuid

from panel_core.extensions import db
from panel_core.models import AccessEntitlement, AccountAccessState, Client, Inbound, ProvisionReceipt, NotificationLog
from panel_core.services.credentials import generate_client_credentials
from panel_core.services.runtime_apply import (
    mark_runtime_dirty,
    prepare_runtime_config,
    runtime_lock,
    synchronize_runtime,
)
from panel_core.services.wireguard import ensure_wireguard_addresses
from panel_core.services.traffic_store import read_traffic_sample, settle_client_traffic, start_traffic_cycle
from panel_core.xray.facade import _api_add_user_grpc, _api_remove_user_grpc
from panel_core.xray.protocol import inbound_supports_vless_flow


def _now_ms():
    return int(time.time() * 1000)


def _logical_key(telegram_id, tariff_id, inbound_tag):
    return f"{telegram_id}:{tariff_id or 0}:{inbound_tag}"


def _account_guard(telegram_id, revision):
    state = db.session.get(AccountAccessState, telegram_id, populate_existing=True)
    if state is not None and (state.blocked or revision < state.revision):
        raise ValueError("account_access_superseded_or_blocked")
    if state is None:
        db.session.add(AccountAccessState(telegram_id=telegram_id, revision=revision, blocked=False))
    elif revision > state.revision:
        state.revision = revision


def _save_usage(client):
    if client.active_entitlement_source:
        source = AccessEntitlement.query.filter_by(
            source_id=client.active_entitlement_source, inbound_tag=client.inbound_tag
        ).first()
        if source is not None:
            source.up, source.down = client.up or 0, client.down or 0


def refresh_client_entitlements(client, *, operation_id=None, sample=None):
    if sample is None:
        sample = read_traffic_sample()
    settle_client_traffic(client, sample=sample)
    _save_usage(client)
    now = _now_ms()
    sources = (
        AccessEntitlement.query.filter_by(client_id=client.id, revoked=False, enabled=True)
        .order_by(AccessEntitlement.created_at.desc(), AccessEntitlement.id.desc())
        .all()
    )
    sources = [source for source in sources if source.expires_at_ms == 0 or source.expires_at_ms > now]
    account = db.session.get(AccountAccessState, client.telegram_id) if client.telegram_id is not None else None
    if not sources:
        client.enable = False
        client.disable_reason = "expiry"
        client.expiry_time = now - 1
        client.active_entitlement_source = None
    else:
        owner = sources[0]
        client.expiry_time = (
            0
            if any(source.expires_at_ms == 0 for source in sources)
            else max(source.expires_at_ms for source in sources)
        )
        client.limit_bytes = owner.limit_bytes
        client.up, client.down = owner.up, owner.down
        client.active_entitlement_source = owner.source_id
        quota_available = not client.limit_bytes or client.up + client.down < client.limit_bytes
        client.enable = quota_available and not client.manual_disabled and not (account and account.blocked)
        client.disable_reason = (
            "" if client.enable else "manual" if client.manual_disabled or (account and account.blocked) else "quota"
        )
    if operation_id:
        client.access_generation = operation_id
        start_traffic_cycle(client, generation=operation_id, sample=sample, reset_usage=False)
    return client


def capture_manual_entitlement(client):
    if not client.provisioning_key or client.telegram_id is None:
        return
    if client.expiry_time is None:
        raise ValueError("manual_expiry_requires_explicit_value")
    sample = read_traffic_sample()
    settle_client_traffic(client, sample=sample)
    _save_usage(client)
    source_id = f"manual:{client.id}"
    source = AccessEntitlement.query.filter_by(source_id=source_id, inbound_tag=client.inbound_tag).first()
    if source is None:
        source = AccessEntitlement(
            source_id=source_id,
            inbound_tag=client.inbound_tag,
            telegram_id=client.telegram_id,
            tariff_id=client.tariff_id,
            client_id=client.id,
            operation_id=f"manual:{uuid.uuid4().hex}",
        )
        db.session.add(source)
    source.source_revision = (source.source_revision or 0) + 1
    source.operation_id = f"manual:{uuid.uuid4().hex}"
    source.created_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    client.access_generation = source.operation_id
    source.expires_at_ms, source.limit_bytes = client.expiry_time, client.limit_bytes or 0
    source.up, source.down = client.up or 0, client.down or 0
    source.revoked, source.enabled = False, bool(client.enable)
    client.manual_disabled = not bool(client.enable)
    client.active_entitlement_source = source_id
    start_traffic_cycle(client, generation=source.operation_id, sample=sample, reset_usage=False)


def _client_for(telegram_id, tariff_id, inbound):
    key = _logical_key(telegram_id, tariff_id, inbound.tag)
    clients = Client.query.filter_by(telegram_id=telegram_id, tariff_id=tariff_id, inbound_tag=inbound.tag).all()
    if len(clients) > 1:
        raise ValueError("duplicate_logical_clients_require_review")
    if clients:
        client = clients[0]
        if client.provisioning_key is None:
            if client.expiry_time is None:
                raise ValueError("legacy_client_expiry_requires_review")
            client.provisioning_key = key
            source_id = f"legacy:{client.id}"
            db.session.add(
                AccessEntitlement(
                    source_id=source_id,
                    operation_id=source_id,
                    telegram_id=telegram_id,
                    tariff_id=tariff_id,
                    inbound_tag=inbound.tag,
                    client_id=client.id,
                    expires_at_ms=client.expiry_time,
                    limit_bytes=client.limit_bytes or 0,
                    up=client.up or 0,
                    down=client.down or 0,
                    enabled=bool(client.enable),
                )
            )
            client.active_entitlement_source = source_id
            client.manual_disabled = not bool(client.enable) and (
                not client.limit_bytes or (client.up or 0) + (client.down or 0) < client.limit_bytes
            )
        return client
    identity = generate_client_credentials(inbound)
    email = f"tg{telegram_id}_{inbound.tag}_{tariff_id or 0}"
    if Client.query.filter_by(inbound_tag=inbound.tag, email=email).first() is not None:
        raise ValueError("duplicate_client_email_requires_review")
    client = Client(
        id=identity,
        email=email,
        inbound_tag=inbound.tag,
        telegram_id=telegram_id,
        tariff_id=tariff_id,
        provisioning_key=key,
        up=0,
        down=0,
        enable=False,
        flow="xtls-rprx-vision" if inbound_supports_vless_flow(inbound) else "",
    )
    db.session.add(client)
    db.session.flush()
    return client


def _sync(callback=None):
    db.session.flush()
    try:
        prepare_runtime_config()
    except Exception:
        db.session.rollback()
        raise
    revision = mark_runtime_dirty()
    db.session.commit()
    synchronize_runtime(callback, expected_revision=revision)


def _apply_provisioned_client(inbound, client, was_enabled):
    if inbound.protocol not in {"vless", "vmess"}:
        return False
    if bool(client.enable) == was_enabled:
        return True
    if client.preferred_outbound:
        return False
    if client.enable:
        return _api_add_user_grpc(inbound.tag, client)
    return _api_remove_user_grpc(inbound.tag, client.email)


def provision(
    *,
    telegram_id,
    inbound_tag,
    limit_bytes,
    expiry_ms=None,
    period_ms=None,
    tariff_id=None,
    idempotency_key=None,
    source_id=None,
    source_revision=0,
    operation_id=None,
    account_revision=0,
):
    operation_id = operation_id or idempotency_key or f"assign:{uuid.uuid4().hex}"
    source_id = source_id or idempotency_key or operation_id
    payload = {
        "telegram_id": telegram_id,
        "inbound_tag": inbound_tag,
        "limit_bytes": limit_bytes,
        "expiry_ms": expiry_ms,
        "period_ms": period_ms,
        "tariff_id": tariff_id,
        "source_id": source_id,
        "source_revision": source_revision,
    }
    with runtime_lock():
        db.session.expire_all()
        _account_guard(telegram_id, account_revision)
        receipt = ProvisionReceipt.query.filter_by(idempotency_key=operation_id, inbound_tag=inbound_tag).first()
        source = AccessEntitlement.query.filter_by(source_id=source_id, inbound_tag=inbound_tag).first()
        if receipt is not None:
            if receipt.request_json != payload:
                raise ValueError("provision_receipt_payload_mismatch")
            result = json.loads(receipt.response_json)
            client = db.session.get(Client, result.get("client", {}).get("id"))
            if (
                client is None
                or source is None
                or source.revoked
                or source.client_id != client.id
                or source.source_revision != source_revision
            ):
                raise ValueError("provision_receipt_access_missing_or_superseded")
            synchronize_runtime()
            receipt.materialized = True
            db.session.commit()
            return {
                "client": client.to_dict(),
                "expires_at_ms": client.expiry_time,
                "source_expires_at_ms": source.expires_at_ms,
                "source_id": source.source_id,
                "source_revision": source.source_revision,
            }
        if source is not None and source.source_revision >= source_revision:
            raise ValueError("entitlement_revision_superseded")
        inbound = Inbound.query.filter_by(tag=inbound_tag).first()
        if inbound is None:
            raise ValueError("inbound_not_found")
        client = _client_for(telegram_id, tariff_id, inbound)
        was_enabled = bool(client.enable)
        sample = read_traffic_sample()
        settle_client_traffic(client, sample=sample)
        _save_usage(client)
        sources = AccessEntitlement.query.filter_by(client_id=client.id, revoked=False, enabled=True).all()
        expiry = expiry_ms
        if period_ms is not None:
            valid = [
                item.expires_at_ms for item in sources if item.expires_at_ms == 0 or item.expires_at_ms > _now_ms()
            ]
            expiry = 0 if 0 in valid else max([_now_ms(), *valid]) + period_ms
        if source is None:
            source = AccessEntitlement(
                source_id=source_id, inbound_tag=inbound_tag, telegram_id=telegram_id, tariff_id=tariff_id
            )
            db.session.add(source)
        elif source.telegram_id != telegram_id or source.tariff_id != tariff_id:
            raise ValueError("entitlement_owner_mismatch")
        source.operation_id, source.source_revision = operation_id, source_revision
        source.created_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        source.client_id, source.expires_at_ms, source.limit_bytes = client.id, expiry, limit_bytes
        source.up, source.down, source.enabled, source.revoked = 0, 0, True, False
        db.session.flush()
        client.active_entitlement_source = None
        refresh_client_entitlements(client, operation_id=operation_id, sample=sample)
        NotificationLog.query.filter(
            NotificationLog.client_id == client.id,
            (NotificationLog.kind.like("expiry_%")) | (NotificationLog.kind == "expired"),
        ).delete(synchronize_session=False)
        ensure_wireguard_addresses(inbound)
        result = {
            "client": client.to_dict(),
            "expires_at_ms": client.expiry_time,
            "source_expires_at_ms": source.expires_at_ms,
            "source_id": source.source_id,
            "source_revision": source.source_revision,
        }
        receipt = ProvisionReceipt(
            idempotency_key=operation_id,
            inbound_tag=inbound_tag,
            telegram_id=telegram_id,
            request_json=payload,
            response_json=json.dumps(result),
            materialized=False,
        )
        db.session.add(receipt)
        _sync(lambda: _apply_provisioned_client(inbound, client, was_enabled))
        receipt.materialized = True
        db.session.commit()
        return result


def revoke_source(
    *, telegram_id, inbound_tag, source_id, source_revision=1, operation_id=None, tariff_id=None, revoked_sources=None
):
    operation_id = operation_id or f"revoke:{source_id}:{source_revision}"
    with runtime_lock():
        db.session.expire_all()
        if source_id == f"tariff:{telegram_id}:{tariff_id}":
            receipt = ProvisionReceipt.query.filter_by(idempotency_key=operation_id, inbound_tag=inbound_tag).first()
            payload = {
                "telegram_id": telegram_id,
                "tariff_id": tariff_id,
                "source_id": source_id,
                "source_revision": source_revision,
                "kind": "revoke",
                "revoked_sources": revoked_sources,
            }
            if receipt is not None:
                if receipt.request_json != payload:
                    raise ValueError("provision_receipt_payload_mismatch")
                synchronize_runtime()
                receipt.materialized = True
                db.session.commit()
                return json.loads(receipt.response_json)
            clients = Client.query.filter_by(
                telegram_id=telegram_id, tariff_id=tariff_id, inbound_tag=inbound_tag
            ).all()
            disabled_count = sum(bool(client.enable) for client in clients)
            for client in clients:
                settle_client_traffic(client)
                _save_usage(client)
                for row in AccessEntitlement.query.filter_by(client_id=client.id).all():
                    row.revoked = True
                    row.source_revision += 1
                client.enable, client.expiry_time = False, _now_ms() - 1
                client.access_generation = operation_id
            for revoked_id, revision in (revoked_sources or {}).items():
                existing = AccessEntitlement.query.filter_by(source_id=revoked_id, inbound_tag=inbound_tag).first()
                if existing is None:
                    db.session.add(
                        AccessEntitlement(
                            source_id=revoked_id,
                            source_revision=revision,
                            operation_id=operation_id,
                            telegram_id=telegram_id,
                            tariff_id=tariff_id,
                            inbound_tag=inbound_tag,
                            revoked=True,
                        )
                    )
            result = {"disabled_clients": disabled_count, "expires_at_ms": None}
            receipt = ProvisionReceipt(
                idempotency_key=operation_id,
                inbound_tag=inbound_tag,
                telegram_id=telegram_id,
                request_json=payload,
                response_json=json.dumps(result),
                materialized=False,
            )
            db.session.add(receipt)
            _sync()
            receipt.materialized = True
            db.session.commit()
            return result
        source = AccessEntitlement.query.filter_by(source_id=source_id, inbound_tag=inbound_tag).first()
        if source is None:
            if (
                Client.query.filter_by(telegram_id=telegram_id, tariff_id=tariff_id, inbound_tag=inbound_tag).first()
                is not None
            ):
                raise ValueError("legacy_entitlement_source_requires_review")
            source = AccessEntitlement(
                source_id=source_id,
                inbound_tag=inbound_tag,
                telegram_id=telegram_id,
                tariff_id=tariff_id,
                operation_id=operation_id,
                source_revision=source_revision,
                revoked=True,
            )
            db.session.add(source)
            db.session.commit()
            return {"disabled_clients": 0, "expires_at_ms": None}
        if source.telegram_id != telegram_id or source.source_revision > source_revision:
            raise ValueError("entitlement_owner_or_revision_mismatch")
        client = db.session.get(Client, source.client_id) if source.client_id else None
        sample = read_traffic_sample()
        if client is not None:
            settle_client_traffic(client, sample=sample)
            _save_usage(client)
        source.revoked, source.source_revision, source.operation_id = True, source_revision, operation_id
        if client is not None:
            refresh_client_entitlements(client, operation_id=operation_id, sample=sample)
            _sync()
        else:
            db.session.commit()
        return {
            "disabled_clients": int(client is not None and not client.enable),
            "expires_at_ms": client.expiry_time if client else None,
        }


def reset_source_cycle(*, telegram_id, inbound_tag, source_id, source_revision, operation_id, tariff_id=None):
    with runtime_lock():
        db.session.expire_all()
        payload = {
            "telegram_id": telegram_id,
            "source_id": source_id,
            "source_revision": source_revision,
            "kind": "reset",
            "tariff_id": tariff_id,
        }
        receipt = ProvisionReceipt.query.filter_by(idempotency_key=operation_id, inbound_tag=inbound_tag).first()
        if receipt is not None:
            if receipt.request_json != payload:
                raise ValueError("provision_receipt_payload_mismatch")
            synchronize_runtime()
            return json.loads(receipt.response_json)
        source = AccessEntitlement.query.filter_by(
            source_id=source_id, inbound_tag=inbound_tag, telegram_id=telegram_id
        ).first()
        if source is None and source_id.startswith("grant:") and tariff_id is not None:
            clients = Client.query.filter_by(
                telegram_id=telegram_id, tariff_id=tariff_id, inbound_tag=inbound_tag
            ).all()
            if len(clients) == 1 and not clients[0].provisioning_key and clients[0].expiry_time is not None:
                client = clients[0]
                settle_client_traffic(client)
                client.provisioning_key = _logical_key(telegram_id, tariff_id, inbound_tag)
                client.active_entitlement_source = source_id
                client.manual_disabled = client.manual_disabled or (
                    not client.enable and client.disable_reason not in {"quota", "expiry"}
                )
                source = AccessEntitlement(
                    source_id=source_id,
                    source_revision=source_revision,
                    operation_id=operation_id,
                    telegram_id=telegram_id,
                    tariff_id=tariff_id,
                    inbound_tag=inbound_tag,
                    client_id=client.id,
                    expires_at_ms=client.expiry_time,
                    limit_bytes=client.limit_bytes,
                    up=client.up,
                    down=client.down,
                    enabled=True,
                    revoked=False,
                )
                db.session.add(source)
                db.session.flush()
        if source is None or source.revoked or source.source_revision != source_revision:
            raise ValueError("entitlement_cycle_requires_review_or_superseded")
        client = db.session.get(Client, source.client_id)
        if client is None:
            raise ValueError("entitlement_client_missing")
        active = client.active_entitlement_source == source_id
        if active:
            start_traffic_cycle(client, generation=operation_id)
            source.up = source.down = 0
            refresh_client_entitlements(client)
        else:
            source.up = source.down = 0
        result = {"reset": int(active), "source_reset": True, "operation_id": operation_id}
        receipt = ProvisionReceipt(
            idempotency_key=operation_id,
            inbound_tag=inbound_tag,
            telegram_id=telegram_id,
            request_json=payload,
            response_json=json.dumps(result),
            materialized=False,
        )
        db.session.add(receipt)
        _sync()
        receipt.materialized = True
        db.session.commit()
        return result


def apply_account_state(*, telegram_id, revision, blocked):
    with runtime_lock():
        db.session.expire_all()
        state = db.session.get(AccountAccessState, telegram_id)
        if state is None:
            state = AccountAccessState(telegram_id=telegram_id, revision=revision, blocked=blocked)
            db.session.add(state)
        elif revision < state.revision or (revision == state.revision and bool(blocked) != state.blocked):
            raise ValueError("account_revision_superseded")
        else:
            state.revision, state.blocked = revision, blocked
        clients = Client.query.filter_by(telegram_id=telegram_id).all()
        previously_enabled = {client.id: bool(client.enable) for client in clients}
        for client in clients:
            if client.provisioning_key:
                refresh_client_entitlements(client)
            elif blocked:
                client.manual_disabled = client.manual_disabled or not bool(client.enable)
                client.enable = False
            else:
                expiry_valid = client.expiry_time is not None and (
                    client.expiry_time == 0 or client.expiry_time > _now_ms()
                )
                quota_valid = not client.limit_bytes or (client.up or 0) + (client.down or 0) < client.limit_bytes
                client.enable = expiry_valid and quota_valid and not client.manual_disabled
        if clients:
            _sync()
        else:
            db.session.commit()
        return {
            "revision": revision,
            "blocked": blocked,
            "affected_clients": len(clients),
            "disabled_clients": sum(previously_enabled[client.id] and not client.enable for client in clients),
            "re_enabled": sum(not previously_enabled[client.id] and client.enable for client in clients),
        }
