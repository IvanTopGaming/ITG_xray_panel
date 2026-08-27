import time

import jwt as jwt_lib
import pytest

from panel_core.models import Admin, SystemSetting
from panel_core.utils import SECRET_KEY


def _admin_token(app):
    from panel_core.extensions import db

    pwd_version = int(time.time())
    admin = Admin(
        username="admin",
        password="hashed-not-checked-by-token-required",
        password_changed_at=pwd_version,
    )
    db.session.add(admin)
    db.session.commit()
    token = jwt_lib.encode(
        {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": pwd_version,
            "exp": int(time.time()) + 3600,
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    return token


def test_instance_id_is_created_once_and_reused(app, db):
    from panel_core.services.node_identity import get_or_create_instance_id

    first = get_or_create_instance_id()
    second = get_or_create_instance_id()

    assert first == second, (
        "повторный вызов обязан вернуть тот же идентификатор, иначе нода будет представляться мастеру новой на каждый запрос"
    )
    assert len(first) >= 32


def test_regenerate_replaces_it(app, db):
    from panel_core.services.node_identity import get_or_create_instance_id, regenerate_instance_id

    before = get_or_create_instance_id()
    after = regenerate_instance_id()

    assert after != before
    assert get_or_create_instance_id() == after


def test_it_lives_in_system_setting(app, db):
    from panel_core.services.node_identity import INSTANCE_SETTING_KEY, get_or_create_instance_id

    value = get_or_create_instance_id()
    row = db.session.get(SystemSetting, INSTANCE_SETTING_KEY)

    assert row is not None and row.value == value


@pytest.fixture
def restore_app(tmp_path):
    from flask import Flask

    from panel_core.api import backup as backup_bp
    from panel_core.app_base import register_readyz
    from panel_core.extensions import db as _db
    from panel_core.models import FederationConfig

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'panel.db'}"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    _db.init_app(flask_app)
    flask_app.register_blueprint(backup_bp.bp, url_prefix="/api")

    with flask_app.app_context():
        _db.create_all()
        _db.session.add(FederationConfig(id=1))
        _db.session.commit()
        register_readyz(flask_app)
        yield flask_app
        _db.session.remove()
        _db.drop_all()


def test_file_restore_mints_a_fresh_instance_id(restore_app, tmp_path, monkeypatch):
    import io
    import sqlite3

    from panel_core.services.node_identity import get_or_create_instance_id

    stale = get_or_create_instance_id()

    donor = tmp_path / "donor.db"
    conn = sqlite3.connect(donor)
    conn.execute("CREATE TABLE system_setting (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO system_setting VALUES ('node_instance_id', 'copied-from-another-box')")
    conn.commit()
    conn.close()

    from panel_core.api import backup as backup_api

    monkeypatch.setattr(backup_api, "generate_config_file", lambda *a, **k: None)
    monkeypatch.setattr(backup_api, "restart_xray_container", lambda *a, **k: None)
    monkeypatch.setattr(backup_api, "_schedule_worker_restart", lambda *a, **k: None)
    monkeypatch.setattr(backup_api, "_db_path", lambda: str(tmp_path / "panel.db"))

    client = restore_app.test_client()
    with restore_app.app_context():
        token = _admin_token(restore_app)

    resp = client.post(
        "/api/restore",
        data={"file": (io.BytesIO(donor.read_bytes()), "backup.db")},
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200, resp.get_json()
    fresh = get_or_create_instance_id()
    assert fresh not in (stale, "copied-from-another-box"), (
        "после заливки чужой базы идентификатор экземпляра обязан быть новым, иначе две машины "
        "будут честно называться одним экземпляром и проверка замещения перестанет различать их"
    )
