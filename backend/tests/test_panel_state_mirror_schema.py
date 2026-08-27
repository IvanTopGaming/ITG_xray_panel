import time

import jwt as jwt_lib
import pytest

from panel_core.models import Admin, LinkedPanel, PanelStateMirror
from panel_core.utils import SECRET_KEY


def _panel(db, name="alpha"):
    panel = LinkedPanel(name=name, url="https://n/x", federation_token="t", created_at=1)
    db.session.add(panel)
    db.session.commit()
    return panel


@pytest.fixture
def app(app):
    from panel_core.api import panels

    if not any(bp_name == "panels" for bp_name in app.blueprints):
        app.register_blueprint(panels.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_token(app, db):
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


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_hot_and_cold_are_written_independently(app, db):
    from panel_core.services.state_mirror import read_current, write_cold, write_hot

    panel = _panel(db)

    write_hot(panel.id, {"inbounds": [1]}, taken_at=100, instance_id="abc", app_version="3.2.0", shrink_flagged=False)
    write_cold(panel.id, {"outbounds": [2]}, fingerprint="f" * 64, taken_at=101)
    write_hot(
        panel.id, {"inbounds": [1, 2]}, taken_at=200, instance_id="abc", app_version="3.2.0", shrink_flagged=False
    )

    row = read_current(panel.id)
    assert row.hot_updated_at == 200
    assert row.cold_updated_at == 101, (
        "холодная половина обязана переживать обновление горячей: иначе мы возим её каждые "
        "10 секунд, то есть строим вариант, который отвергли"
    )
    assert row.cold_fingerprint == "f" * 64


def test_only_one_current_row_per_panel(app, db):
    from panel_core.services.state_mirror import write_hot

    panel = _panel(db)
    for taken_at in (1, 2, 3):
        write_hot(
            panel.id, {"inbounds": []}, taken_at=taken_at, instance_id="a", app_version="3.2.0", shrink_flagged=False
        )

    assert PanelStateMirror.query.filter_by(panel_id=panel.id, kind="current").count() == 1


def test_archive_copies_the_current_row_and_prunes_by_age(app, db):
    from panel_core.services.state_mirror import archive_daily, prune_archive, write_cold, write_hot

    panel = _panel(db)
    write_hot(panel.id, {"inbounds": [1]}, taken_at=100, instance_id="a", app_version="3.2.0", shrink_flagged=False)
    write_cold(panel.id, {"outbounds": []}, fingerprint="f" * 64, taken_at=100)

    archive_daily(panel.id, taken_at=1_000)
    archive_daily(panel.id, taken_at=9_000)

    assert PanelStateMirror.query.filter_by(panel_id=panel.id, kind="daily").count() == 2
    assert prune_archive(older_than_ms=5_000) == 1
    assert PanelStateMirror.query.filter_by(panel_id=panel.id, kind="daily").count() == 1
    assert PanelStateMirror.query.filter_by(panel_id=panel.id, kind="current").count() == 1, (
        "уборка архива не должна трогать текущую копию"
    )


def test_forget_mirror_removes_everything_for_that_panel(app, db):
    from panel_core.services.state_mirror import archive_daily, forget_mirror, write_hot

    panel = _panel(db)
    write_hot(panel.id, {"inbounds": []}, taken_at=1, instance_id="a", app_version="3.2.0", shrink_flagged=False)
    archive_daily(panel.id, taken_at=2)

    forget_mirror(panel.id)

    assert PanelStateMirror.query.filter_by(panel_id=panel.id).count() == 0


def test_linked_panel_has_the_transfer_columns(app, db):
    panel = _panel(db)

    assert panel.transfer_token_used is False
    assert panel.transfer_carry_admin is True
    assert panel.transfer_state == ""
    assert panel.current_instance_id is None
    assert panel.to_dict()["transfer_state"] == ""
    assert "transfer_token" not in panel.to_dict(), (
        "ключ переноса по силе равен federation-токену и в списке панелей светиться не должен"
    )


def test_sqlite_migration_reaches_28(tmp_path):
    from panel_core.db_migration import CURRENT_DB_VERSION, migrate_sqlite_db

    report = migrate_sqlite_db(str(tmp_path / "db" / "panel.db"), seed_bot_texts=False)

    assert CURRENT_DB_VERSION == 28
    assert report["new_version"] == 28
    report_again = migrate_sqlite_db(str(tmp_path / "db" / "panel.db"), seed_bot_texts=False)
    assert report_again["new_version"] == 28, "миграция обязана быть идемпотентной"


def test_deleting_a_panel_forgets_its_mirror(client, admin_token, db):
    from panel_core.services.state_mirror import write_hot

    panel = _panel(db)
    write_hot(panel.id, {"inbounds": []}, taken_at=1, instance_id="a", app_version="3.2.0", shrink_flagged=False)

    resp = client.delete(f"/api/panels/{panel.id}", headers=_auth(admin_token))

    assert resp.status_code == 200
    assert PanelStateMirror.query.filter_by(panel_id=panel.id).count() == 0
