import secrets
from sqlalchemy import text

from panel_core.extensions import db
from panel_core.models import SystemSetting

INSTANCE_SETTING_KEY = "node_instance_id"


def _write(value: str) -> str:
    row = db.session.get(SystemSetting, INSTANCE_SETTING_KEY)
    if row is None:
        row = SystemSetting(key=INSTANCE_SETTING_KEY, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()
    return value


def get_or_create_instance_id() -> str:
    row = db.session.get(SystemSetting, INSTANCE_SETTING_KEY)
    if row is not None and (row.value or "").strip():
        return row.value.strip()
    value = secrets.token_hex(16)
    db.session.execute(
        text("INSERT INTO system_setting (key, value) VALUES (:key, :value) ON CONFLICT (key) DO NOTHING"),
        {"key": INSTANCE_SETTING_KEY, "value": value},
    )
    db.session.execute(
        text("UPDATE system_setting SET value = :value WHERE key = :key AND (value IS NULL OR trim(value) = '')"),
        {"key": INSTANCE_SETTING_KEY, "value": value},
    )
    db.session.commit()
    return db.session.get(SystemSetting, INSTANCE_SETTING_KEY).value.strip()


def regenerate_instance_id() -> str:
    return _write(secrets.token_hex(16))
