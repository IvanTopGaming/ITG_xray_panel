import hashlib
import time
from contextlib import contextmanager
from typing import Tuple

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from panel_core.extensions import db
from panel_core.models import UserDevice

GateState = str


@contextmanager
def _registration_session(telegram_id):
    with db.engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        timeout = None
        if sqlite:
            timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
            connection.exec_driver_sql("PRAGMA busy_timeout=0")
            connection.commit()
        try:
            with Session(bind=connection) as session, session.begin():
                if sqlite:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            session.execute(text("BEGIN IMMEDIATE"))
                            break
                        except OperationalError as exc:
                            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                                raise
                            time.sleep(0.01)
                elif connection.dialect.name == "postgresql":
                    identity = hashlib.sha256(f"panel:device-budget:{telegram_id}".encode()).digest()[:8]
                    session.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": int.from_bytes(identity, "big", signed=True)},
                    )
                else:
                    raise RuntimeError("Device registration requires SQLite or PostgreSQL transaction locking")
                yield session
        finally:
            if sqlite:
                connection.exec_driver_sql(f"PRAGMA busy_timeout={int(timeout)}")
                connection.rollback()


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

    with db.session.no_autoflush:
        enabled, limit = subscription_device_settings()
    if not enabled:
        return ("ok", {})

    hwid = (headers.get("x-hwid") or "").strip()
    if len(hwid) > 128:
        raise ValueError("Device identifier must not exceed 128 characters")
    if not hwid:
        if limit > 0:
            return ("unsupported", {"x-hwid-active": "true", "x-hwid-not-supported": "true"})
        return ("ok", {})

    base_headers = {"x-hwid-active": "true"}
    now_ms = int(time.time() * 1000)

    with _registration_session(telegram_id) as session:
        existing = session.query(UserDevice).filter_by(telegram_id=telegram_id, hwid=hwid).first()
        if existing:
            existing.last_seen = now_ms
            existing.hits = (existing.hits or 0) + 1
            if headers.get("_request_ip"):
                existing.request_ip = headers["_request_ip"][:64]
            if headers.get("user-agent"):
                existing.user_agent = headers["user-agent"][:512]
            return ("ok", base_headers)
        if limit > 0 and session.query(UserDevice).filter_by(telegram_id=telegram_id).count() >= limit:
            return ("limit", {**base_headers, "x-hwid-max-devices-reached": "true"})
        session.add(
            UserDevice(
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
        )
    return ("ok", base_headers)
