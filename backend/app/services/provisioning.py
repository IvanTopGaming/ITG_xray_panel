"""Single gateway for tariff grants: YooKassa webhook, admin grant, trial, auto-renew."""

import logging
import secrets
import time
import uuid
from typing import TYPE_CHECKING

from app.extensions import db
from app.models import Client, Inbound, LinkedPanel, NotificationLog
from app.services import sub_cache
from app.services.panel_proxy import fetch_panel_snapshot_live
from app.services.stats import _api_add_user_grpc
from app.services.xray import generate_config_file, restart_xray_container

if TYPE_CHECKING:
    from app.models import Tariff, TariffItem

logger = logging.getLogger(__name__)

_GB = 1024**3


def _sync_after_provision(
    new_clients: list,
    extended_clients_with_state: list,
) -> None:
    """Sync Xray runtime after a provisioning batch.

    `extended_clients_with_state` is a list of `(Client, was_enabled_before)` pairs.
    Fast-path: when every client lives on a vless/vmess inbound, patch the runtime
    via `_api_add_user_grpc` instead of restarting. New clients always need AddUser.
    Extended clients only need AddUser if they were previously disabled — when they
    were already enabled, their id/email haven't changed and the runtime still has
    them. Falls back to `restart_xray_container` if any inbound is non-vless/vmess
    or any gRPC call fails.
    """
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
    # Caller must check uniqueness on (inbound_tag, email) and add a suffix on collision.
    return f"tg{telegram_id}_{inbound_tag}"


def _create_client_for_item(
    *,
    telegram_id: int,
    tariff: "Tariff",
    item: "TariffItem",
    expiry_ms: int,
    limit_bytes: int,
) -> Client:
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
        flow="xtls-rprx-vision" if inbound.protocol == "vless" else "",
    )
    db.session.add(client)
    return client


def provision_single_item(
    *,
    telegram_id: int,
    inbound_tag: str,
    expiry_ms: int,
    limit_bytes: int,
    tariff_id: int | None = None,
) -> dict:
    """Create or extend a single Client by (telegram_id, inbound_tag). Used by federation provision endpoint."""
    now_ms = int(time.time() * 1000)
    inbound = Inbound.query.filter_by(tag=inbound_tag).first()
    if inbound is None:
        raise ValueError(f"Inbound {inbound_tag!r} not found")

    client = Client.query.filter_by(telegram_id=telegram_id, inbound_tag=inbound_tag).first()

    new_clients: list[Client] = []
    extended_clients_with_state: list[tuple[Client, bool]] = []

    if client is not None:
        was_enabled = bool(client.enable)
        client.expiry_time = expiry_ms
        client.limit_bytes = limit_bytes
        client.up = 0
        client.down = 0
        client.last_reset_time = now_ms
        client.enable = True
        if tariff_id is not None:
            client.tariff_id = tariff_id
        NotificationLog.query.filter(
            NotificationLog.client_id == client.id,
            NotificationLog.kind.in_(("traffic_80", "traffic_95", "traffic_exhausted")),
        ).delete(synchronize_session=False)
        extended_clients_with_state.append((client, was_enabled))
    else:
        identity = _generate_identity(inbound.protocol)
        base_email = _generate_email(telegram_id, inbound_tag)
        email = base_email
        for _attempt in range(8):
            if not Client.query.filter_by(inbound_tag=inbound_tag, email=email).first():
                break
            email = f"{base_email}_{secrets.token_hex(3)}"
        else:
            raise RuntimeError(f"Could not find unique email for tg={telegram_id}")

        client = Client(
            id=identity,
            email=email,
            inbound_tag=inbound_tag,
            telegram_id=telegram_id,
            tariff_id=tariff_id,
            limit_bytes=limit_bytes,
            expiry_time=expiry_ms,
            up=0,
            down=0,
            enable=True,
            flow="xtls-rprx-vision" if inbound.protocol == "vless" else "",
        )
        db.session.add(client)
        new_clients.append(client)

    db.session.commit()
    _sync_after_provision(new_clients, extended_clients_with_state)

    return {"client": client.to_dict(), "expires_at_ms": expiry_ms}


def apply_tariff_for_user(
    telegram_id: int,
    tariff: "Tariff",
    *,
    source: str,
) -> dict:
    """Extend an existing Client for (tg, inbound) or create one. Expiry stacks: max(now, existing) + period."""
    period_ms = tariff.period_days * 86400_000
    now_ms = int(time.time() * 1000)

    existing = list(Client.query.filter_by(telegram_id=telegram_id).all())
    existing_max_expiry = max((c.expiry_time for c in existing), default=0)
    # Wall-clock semantics: a key bought at HH:MM expires at HH:MM `period_days`
    # later. No calendar-day snap — admins were confused when a 24h tariff
    # purchased at 23:15 expired at 12:00 the next day.
    new_expiry_ms = max(now_ms, existing_max_expiry) + period_ms

    new_clients = []
    extended_clients_with_state: list[tuple[Client, bool]] = []
    for item in tariff.items:
        limit_bytes = item.traffic_gb * _GB if item.traffic_gb else 0

        if item.panel_id is not None:
            from app.services.panel_proxy import proxy_provision

            try:
                proxy_provision(
                    item.panel_id,
                    telegram_id,
                    item.inbound_tag,
                    {"expiry_ms": new_expiry_ms, "limit_bytes": limit_bytes, "tariff_id": tariff.id},
                )
            except Exception as exc:
                logger.error("proxy_provision failed for panel=%s tag=%s: %s", item.panel_id, item.inbound_tag, exc)
                raise
            continue

        client = next(
            (c for c in existing if c.inbound_tag == item.inbound_tag),
            None,
        )
        if client is not None:
            was_enabled = bool(client.enable)
            client.expiry_time = new_expiry_ms
            client.limit_bytes = limit_bytes
            client.up = 0
            client.down = 0
            client.last_reset_time = now_ms
            client.enable = True
            client.tariff_id = tariff.id
            # Counters reset → re-arm traffic warnings (expiry warnings deliberately preserved).
            NotificationLog.query.filter(
                NotificationLog.client_id == client.id,
                NotificationLog.kind.in_(("traffic_80", "traffic_95", "traffic_exhausted")),
            ).delete(synchronize_session=False)
            extended_clients_with_state.append((client, was_enabled))
        else:
            new_client = _create_client_for_item(
                telegram_id=telegram_id,
                tariff=tariff,
                item=item,
                expiry_ms=new_expiry_ms,
                limit_bytes=limit_bytes,
            )
            new_clients.append(new_client)

    db.session.commit()
    _sync_after_provision(new_clients, extended_clients_with_state)
    sub_cache.invalidate_user_aggregate(telegram_id)
    logger.info(
        "provisioned tg=%s tariff=%s source=%s new=%d extended=%d",
        telegram_id,
        tariff.id,
        source,
        len(new_clients),
        len(extended_clients_with_state),
    )

    all_provisioned = new_clients + [c for c, _ in extended_clients_with_state]
    return {
        "clients": [c.to_dict() for c in all_provisioned],
        "expires_at_ms": new_expiry_ms,
        "source": source,
    }


def _collect_tariff_holders(tariff: "Tariff", now_ms: int):
    """Discover active holders of `tariff` across the master and every child
    panel its items route to. Returns (holders, unreachable_ids).

    holders: {telegram_id: {"expiry_ms": int, "have": set[(panel_id|None, inbound_tag)]}}
      - inherited expiry: 0 if any active tariff client is unlimited, else max.
      - have: every (panel_id, inbound_tag) the holder already has a key on (any tariff).
    unreachable_ids: set of child panel_ids whose snapshot could not be fetched.
    """
    records = []
    for c in Client.query.filter(Client.telegram_id.isnot(None)).all():
        active = bool(c.enable) and (c.expiry_time == 0 or c.expiry_time > now_ms)
        records.append((c.telegram_id, None, c.inbound_tag, c.tariff_id, active, c.expiry_time or 0))

    unreachable_ids: set[int] = set()
    child_panel_ids = {it.panel_id for it in tariff.items if it.panel_id is not None}
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
                exp = cl.get("expiry_time") or 0
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

    for tg, panel_id, tag, _tid, _active, _exp in records:
        if tg in holders:
            holders[tg]["have"].add((panel_id, tag))

    return holders, unreachable_ids


def backfill_tariff(tariff: "Tariff") -> dict:
    """Idempotently ensure every active holder of `tariff` has a key on every
    tariff inbound, across the master and its child panels. Never modifies
    existing keys. Returns a summary; unreachable panels are reported, not fatal.
    Failures are counted in `provision_failures`, not raised — re-run (re-save the
    tariff) to retry them.
    """
    now_ms = int(time.time() * 1000)

    panel_names: dict[int, str] = {}
    panel_ids = {it.panel_id for it in tariff.items if it.panel_id is not None}
    if panel_ids:
        for p in LinkedPanel.query.filter(LinkedPanel.id.in_(panel_ids)).all():
            panel_names[p.id] = p.name

    holders, unreachable_ids = _collect_tariff_holders(tariff, now_ms)

    summary = {
        "holders": len(holders),
        "created_local": 0,
        "created_remote": 0,
        "skipped_existing": 0,
        "provision_failures": 0,
        "panels_unreachable": sorted(panel_names.get(pid, str(pid)) for pid in unreachable_ids),
    }

    new_local_clients: list[Client] = []
    for item in tariff.items:
        if item.panel_id is not None and item.panel_id in unreachable_ids:
            continue
        limit_bytes = item.traffic_gb * _GB if item.traffic_gb else 0
        for tg, info in holders.items():
            if (item.panel_id, item.inbound_tag) in info["have"]:
                summary["skipped_existing"] += 1
                continue
            try:
                if item.panel_id is None:
                    new_local_clients.append(
                        _create_client_for_item(
                            telegram_id=tg,
                            tariff=tariff,
                            item=item,
                            expiry_ms=info["expiry_ms"],
                            limit_bytes=limit_bytes,
                        )
                    )
                    summary["created_local"] += 1
                else:
                    from app.services.panel_proxy import proxy_provision

                    proxy_provision(
                        item.panel_id,
                        tg,
                        item.inbound_tag,
                        {"expiry_ms": info["expiry_ms"], "limit_bytes": limit_bytes, "tariff_id": tariff.id},
                    )
                    summary["created_remote"] += 1
            except Exception as exc:
                logger.error(
                    "backfill provision failed tariff=%s panel=%s tag=%s tg=%s: %s",
                    tariff.id,
                    item.panel_id,
                    item.inbound_tag,
                    tg,
                    exc,
                )
                summary["provision_failures"] += 1

    db.session.commit()
    _sync_after_provision(new_local_clients, [])
    logger.info(
        "backfill_tariff tariff=%s holders=%d created_local=%d created_remote=%d skipped=%d failures=%d unreachable=%s",
        tariff.id,
        summary["holders"],
        summary["created_local"],
        summary["created_remote"],
        summary["skipped_existing"],
        summary["provision_failures"],
        summary["panels_unreachable"],
    )
    return summary
