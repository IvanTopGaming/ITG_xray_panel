"""Single gateway for tariff grants: YooKassa webhook, admin grant, trial, auto-renew."""

import datetime as _dt
import logging
import secrets
import time
import uuid
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.extensions import db
from app.models import Client, Inbound, NotificationLog, SystemSetting
from app.services import sub_cache
from app.services.xray import generate_config_file, restart_xray_container

if TYPE_CHECKING:
    from app.models import Tariff, TariffItem

logger = logging.getLogger(__name__)

_GB = 1024**3


def _display_tz() -> ZoneInfo:
    row = SystemSetting.query.filter_by(key="display_timezone").first()
    name = row.value if row and row.value else "Europe/Moscow"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def _align_to_noon_in_tz(epoch_ms: int) -> int:
    """Snap to 12:00 local on the same calendar day — predictable expiry wall-clock regardless of purchase time."""
    tz = _display_tz()
    local = _dt.datetime.fromtimestamp(epoch_ms / 1000, tz=tz)
    noon_local = local.replace(hour=12, minute=0, second=0, microsecond=0)
    return int(noon_local.timestamp() * 1000)


def _sync_after_provision(
    new_clients: list,
    extended_clients: list,
) -> None:
    # sub_cache errors are swallowed — local DB is source of truth.
    # Xray regen errors propagate — a broken local Xray is worth surfacing.
    for c in new_clients + extended_clients:
        try:
            sub_cache.invalidate_user(c.id)
        except Exception as exc:
            logger.warning("sub_cache.invalidate_user failed: %s", exc)

    generate_config_file()
    restart_xray_container()


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

    if client is not None:
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

    db.session.commit()
    generate_config_file()
    restart_xray_container()
    try:
        sub_cache.invalidate_user(client.id)
    except Exception:
        pass

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
    new_expiry_ms = _align_to_noon_in_tz(max(now_ms, existing_max_expiry) + period_ms)

    new_clients = []
    extended_clients = []
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
                logger.warning("proxy_provision failed for panel=%s tag=%s: %s", item.panel_id, item.inbound_tag, exc)
                raise
            continue

        client = next(
            (c for c in existing if c.inbound_tag == item.inbound_tag),
            None,
        )
        if client is not None:
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
            extended_clients.append(client)
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
    _sync_after_provision(new_clients, extended_clients)

    return {
        "clients": [c.to_dict() for c in (new_clients + extended_clients)],
        "expires_at_ms": new_expiry_ms,
        "source": source,
    }
