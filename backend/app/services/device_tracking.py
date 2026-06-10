import time
from typing import Tuple

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Client, ClientDevice, Inbound

GateState = str


def effective_device_limit(client: Client, inbound: Inbound) -> int:

    if client.device_limit is not None:
        return int(client.device_limit)
    return int(inbound.device_limit or 0)


def subscription_device_settings():

    from app.models import SystemSetting

    enabled_row = SystemSetting.query.filter_by(key="device_limit_enabled").first()
    enabled = bool(enabled_row and enabled_row.value == "true")
    limit_row = SystemSetting.query.filter_by(key="device_limit_per_user").first()
    try:
        limit = int(limit_row.value) if limit_row and limit_row.value else 0
        if limit < 0:
            limit = 0
    except (ValueError, TypeError):
        limit = 0
    return enabled, limit


def list_devices(client_id: str):
    return ClientDevice.query.filter_by(client_id=client_id).order_by(ClientDevice.last_seen.desc()).all()


def revoke_device(client_id: str, device_id: int) -> bool:

    row = ClientDevice.query.filter_by(id=device_id, client_id=client_id).first()
    if not row:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def device_gate(client: Client, inbound: Inbound, headers: dict) -> Tuple[GateState, dict]:

    limit = effective_device_limit(client, inbound)
    hwid = (headers.get("x-hwid") or "").strip()

    if not hwid:
        if limit > 0:
            return (
                "unsupported",
                {"x-hwid-active": "true", "x-hwid-not-supported": "true"},
            )

        return ("ok", {})

    base_headers = {"x-hwid-active": "true"}
    now_ms = int(time.time() * 1000)

    locked = db.session.query(Client).filter_by(id=client.id).with_for_update().first()
    if locked is None:
        return ("ok", base_headers)

    existing = ClientDevice.query.filter_by(client_id=client.id, hwid=hwid).first()
    if existing:
        existing.last_seen = now_ms
        existing.hits = (existing.hits or 0) + 1
        ip = headers.get("_request_ip")
        if ip:
            existing.request_ip = ip[:64]
        ua = headers.get("user-agent")
        if ua:
            existing.user_agent = ua[:512]
        db.session.commit()
        return ("ok", base_headers)

    if limit > 0:
        count = ClientDevice.query.filter_by(client_id=client.id).count()
        if count >= limit:
            db.session.commit()
            return ("limit", {**base_headers, "x-hwid-max-devices-reached": "true"})

    device = ClientDevice(
        client_id=client.id,
        hwid=hwid,
        device_os=(headers.get("x-device-os") or "")[:32],
        os_ver=(headers.get("x-ver-os") or "")[:32],
        model=(headers.get("x-device-model") or "")[:128],
        user_agent=(headers.get("user-agent") or "")[:512],
        request_ip=(headers.get("_request_ip") or "")[:64],
        first_seen=now_ms,
        last_seen=now_ms,
        hits=1,
    )
    db.session.add(device)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = ClientDevice.query.filter_by(client_id=client.id, hwid=hwid).first()
        if existing:
            existing.last_seen = now_ms
            existing.hits = (existing.hits or 0) + 1
            db.session.commit()
    return ("ok", base_headers)


def _primary_client_id_for_user(telegram_id):

    rows = Client.query.filter_by(telegram_id=telegram_id, enable=True).with_entities(Client.id).all()
    ids = sorted(r[0] for r in rows if r[0])
    return ids[0] if ids else None


def _user_distinct_hwids(telegram_id):

    rows = (
        ClientDevice.query.join(Client, ClientDevice.client_id == Client.id)
        .filter(Client.telegram_id == telegram_id, Client.enable.is_(True))
        .with_entities(ClientDevice.hwid)
        .all()
    )
    return {r[0] for r in rows if r[0]}


def user_device_gate(telegram_id, headers: dict):

    enabled, limit = subscription_device_settings()
    if not enabled:
        return ("ok", {})

    hwid = (headers.get("x-hwid") or "").strip()
    if not hwid:
        if limit > 0:
            return ("unsupported", {"x-hwid-active": "true", "x-hwid-not-supported": "true"})
        return ("ok", {})

    primary = _primary_client_id_for_user(telegram_id)
    if primary is None:
        return ("ok", {})

    base_headers = {"x-hwid-active": "true"}
    now_ms = int(time.time() * 1000)

    existing = (
        ClientDevice.query.join(Client, ClientDevice.client_id == Client.id)
        .filter(Client.telegram_id == telegram_id, ClientDevice.hwid == hwid)
        .first()
    )
    if existing:
        existing.last_seen = now_ms
        existing.hits = (existing.hits or 0) + 1
        ip = headers.get("_request_ip")
        if ip:
            existing.request_ip = ip[:64]
        ua = headers.get("user-agent")
        if ua:
            existing.user_agent = ua[:512]
        db.session.commit()
        return ("ok", base_headers)

    if limit > 0 and len(_user_distinct_hwids(telegram_id)) >= limit:
        return ("limit", {**base_headers, "x-hwid-max-devices-reached": "true"})

    device = ClientDevice(
        client_id=primary,
        hwid=hwid,
        device_os=(headers.get("x-device-os") or "")[:32],
        os_ver=(headers.get("x-ver-os") or "")[:32],
        model=(headers.get("x-device-model") or "")[:128],
        user_agent=(headers.get("user-agent") or "")[:512],
        request_ip=(headers.get("_request_ip") or "")[:64],
        first_seen=now_ms,
        last_seen=now_ms,
        hits=1,
    )
    db.session.add(device)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = ClientDevice.query.filter_by(client_id=primary, hwid=hwid).first()
        if existing:
            existing.last_seen = now_ms
            existing.hits = (existing.hits or 0) + 1
            db.session.commit()
    return ("ok", base_headers)
