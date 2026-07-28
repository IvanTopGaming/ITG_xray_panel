import time
from typing import Tuple

from sqlalchemy.exc import IntegrityError

from panel_core.extensions import db
from panel_core.models import UserDevice

GateState = str


def subscription_device_settings():

    from panel_core.models import SystemSetting

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


def list_user_devices(telegram_id):
    if not telegram_id:
        return []
    return UserDevice.query.filter_by(telegram_id=telegram_id).order_by(UserDevice.last_seen.desc()).all()


def count_user_devices(telegram_id):
    if not telegram_id:
        return 0
    return UserDevice.query.filter_by(telegram_id=telegram_id).count()


def device_counts_by_user():

    from sqlalchemy import func

    rows = db.session.query(UserDevice.telegram_id, func.count(UserDevice.id)).group_by(UserDevice.telegram_id).all()
    return {tg: int(count) for tg, count in rows if tg}


def revoke_user_device(telegram_id, device_id: int) -> bool:

    row = UserDevice.query.filter_by(id=device_id, telegram_id=telegram_id).first()
    if not row:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def user_device_gate(telegram_id, headers: dict) -> Tuple[GateState, dict]:

    if not telegram_id:
        return ("ok", {})

    enabled, limit = subscription_device_settings()
    if not enabled:
        return ("ok", {})

    hwid = (headers.get("x-hwid") or "").strip()
    if not hwid:
        if limit > 0:
            return ("unsupported", {"x-hwid-active": "true", "x-hwid-not-supported": "true"})
        return ("ok", {})

    base_headers = {"x-hwid-active": "true"}
    now_ms = int(time.time() * 1000)

    existing = UserDevice.query.filter_by(telegram_id=telegram_id, hwid=hwid).first()
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

    if limit > 0 and count_user_devices(telegram_id) >= limit:
        return ("limit", {**base_headers, "x-hwid-max-devices-reached": "true"})

    device = UserDevice(
        telegram_id=telegram_id,
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
        existing = UserDevice.query.filter_by(telegram_id=telegram_id, hwid=hwid).first()
        if existing:
            existing.last_seen = now_ms
            existing.hits = (existing.hits or 0) + 1
            db.session.commit()
    return ("ok", base_headers)
