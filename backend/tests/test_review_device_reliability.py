import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask
from sqlalchemy import event

from panel_core.extensions import db
from panel_core.models import SystemSetting, UserDevice
from panel_core.services.device_tracking import user_device_gate


@pytest.fixture
def ledger_app(tmp_path):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'devices.db'}"
    db.init_app(app)
    with app.app_context():
        db.create_all()
        db.session.add_all(
            [
                SystemSetting(key="device_limit_enabled", value="true"),
                SystemSetting(key="device_limit_per_user", value="1"),
            ]
        )
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def test_concurrent_new_devices_share_one_atomic_budget(ledger_app):
    ready = threading.Barrier(2)
    counted = threading.Barrier(2)
    with ledger_app.app_context():
        engine = db.engine

    def interleave_counts(connection, cursor, statement, parameters, context, executemany):
        if "count(" in statement.lower() and "user_device" in statement.lower():
            try:
                counted.wait(timeout=0.3)
            except threading.BrokenBarrierError:
                pass

    def register(hwid):
        with ledger_app.app_context():
            ready.wait(timeout=5)
            return user_device_gate(100, {"x-hwid": hwid})[0]

    event.listen(engine, "after_cursor_execute", interleave_counts)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            result = list(executor.map(register, ["device-a", "device-b"]))
    finally:
        event.remove(engine, "after_cursor_execute", interleave_counts)
    assert sorted(result) == ["limit", "ok"]
    with ledger_app.app_context():
        assert UserDevice.query.filter_by(telegram_id=100).count() == 1


def test_gate_does_not_commit_callers_unrelated_transaction(ledger_app):
    with ledger_app.app_context():
        db.session.add(SystemSetting(key="caller-uncommitted", value="must roll back"))
        assert user_device_gate(100, {"x-hwid": "device-a"})[0] == "ok"
        db.session.rollback()
        assert db.session.get(SystemSetting, "caller-uncommitted") is None
        assert UserDevice.query.filter_by(telegram_id=100).count() == 1


def test_existing_device_survives_limit_reduction(ledger_app):
    with ledger_app.app_context():
        assert user_device_gate(100, {"x-hwid": "device-a"})[0] == "ok"
        assert user_device_gate(100, {"x-hwid": "device-a"})[0] == "ok"
        assert user_device_gate(100, {"x-hwid": "device-b"})[0] == "limit"
        row = UserDevice.query.one()
        assert row.hits == 2


@pytest.fixture
def postgres_ledger_app():
    import os
    from sqlalchemy import text
    from panel_core.pg_migrate import migrate_postgres_db

    dsn = os.getenv("DATABASE_URL_TEST")
    if not dsn:
        pytest.skip("isolated PostgreSQL DSN not configured")
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = dsn
    db.init_app(app)
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE"))
        db.session.execute(text("CREATE SCHEMA public"))
        db.session.commit()
        migrate_postgres_db()
        db.session.add_all(
            [
                SystemSetting(key="device_limit_enabled", value="true"),
                SystemSetting(key="device_limit_per_user", value="1"),
            ]
        )
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def test_postgres_concurrent_devices_share_atomic_budget(postgres_ledger_app):
    test_concurrent_new_devices_share_one_atomic_budget(postgres_ledger_app)


def test_postgres_device_gate_preserves_caller_transaction(postgres_ledger_app):
    test_gate_does_not_commit_callers_unrelated_transaction(postgres_ledger_app)
