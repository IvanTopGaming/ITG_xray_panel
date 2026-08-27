import secrets

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
    return _write(secrets.token_hex(16))


def regenerate_instance_id() -> str:
    return _write(secrets.token_hex(16))
