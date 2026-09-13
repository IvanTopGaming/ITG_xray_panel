import sqlite3
import os

import pytest
from flask import Flask
from sqlalchemy import Column, JSON
from sqlalchemy.dialects import postgresql

from panel_core.db_migration import migrate_sqlite_db
from panel_core.extensions import db


def test_standalone_migration_builds_a_complete_queryable_schema(tmp_path):
    database = str(tmp_path / "fresh.db")
    migrate_sqlite_db(database, seed_bot_texts=False)
    with sqlite3.connect(database) as connection:
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"admin", "client", "inbound", "outbound", "system_setting"} <= present
        for name in ("poll_generation", "poll_applied_generation", "transfer_state_json"):
            connection.execute(f'SELECT "{name}" FROM linked_panel LIMIT 1')
        columns = {row[1] for row in connection.execute("PRAGMA table_info(linked_panel)")}
        assert {"poll_generation", "poll_applied_generation", "transfer_state_json"} <= columns


def test_legacy_bot_text_upgrade_marks_edits_before_seed(tmp_path):
    database = str(tmp_path / "legacy.db")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE bot_text (key TEXT, lang TEXT, text TEXT NOT NULL, updated_at DATETIME, PRIMARY KEY(key,lang))"
        )
        connection.execute("INSERT INTO bot_text VALUES ('welcome.title','ru','Мой текст',CURRENT_TIMESTAMP)")
        connection.commit()
    migrate_sqlite_db(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT text,customized FROM bot_text WHERE key='welcome.title' AND lang='ru'"
        ).fetchone() == ("Мой текст", 1)


def test_startup_rejects_incomplete_schema_despite_admin_table(tmp_path):
    from panel_core.app_base import _require_schema

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'incomplete.db'}"
    db.init_app(app)
    with app.app_context():
        with db.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE admin (id INTEGER PRIMARY KEY, username TEXT, password TEXT)")
        with pytest.raises(RuntimeError, match="schema"):
            _require_schema()
        db.session.remove()
        db.engine.dispose()


def test_newer_schema_is_refused_without_changing_database(tmp_path):
    database = str(tmp_path / "future.db")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=999")
        connection.execute("CREATE TABLE retained (value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('original')")
        connection.commit()
    with pytest.raises(ValueError, match="newer"):
        migrate_sqlite_db(database, seed_bot_texts=False)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999
        assert connection.execute("SELECT value FROM retained").fetchone()[0] == "original"
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 1


def test_postgres_json_defaults_are_sql_literals():
    from panel_core.pg_migrate import _column_ddl

    ddl, nullable = _column_ddl(
        Column("payload", JSON, nullable=False, server_default="{}"), dialect=postgresql.dialect()
    )
    assert "DEFAULT '{}'" in ddl
    assert "NOT NULL" in ddl
    assert nullable is False


def test_fresh_migration_satisfies_startup_constraint_validation(tmp_path):
    from panel_core.app_base import _require_schema

    database = str(tmp_path / "startup.db")
    migrate_sqlite_db(database, seed_bot_texts=False)
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{database}"
    db.init_app(app)
    with app.app_context():
        _require_schema()
        db.session.remove()
        db.engine.dispose()


def test_legacy_payment_backfill_preserves_refund_uncertainty(tmp_path):
    database = str(tmp_path / "payment.db")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE payment (id INTEGER PRIMARY KEY, telegram_id BIGINT NOT NULL, tariff_id INTEGER, amount FLOAT NOT NULL, yookassa_id VARCHAR(64) UNIQUE NOT NULL, status VARCHAR(16), created_at DATETIME, paid_at DATETIME, confirmation_url TEXT)"
        )
        connection.executemany(
            "INSERT INTO payment (id, telegram_id, amount, yookassa_id, status) VALUES (?,42,100,?,?)",
            [
                (1, "paid", "succeeded"),
                (2, "refunded", "refunded"),
                (3, "processing", "processing"),
                (4, "pending-old", "pending"),
            ],
        )
        connection.execute("PRAGMA user_version=28")
    migrate_sqlite_db(database, seed_bot_texts=False)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT provider_status,fulfillment_status FROM payment WHERE id=1").fetchone() == (
            "succeeded",
            "succeeded",
        )
        assert connection.execute("SELECT refund_status,refunded_amount_kopeks FROM payment WHERE id=2").fetchone() == (
            "none",
            0,
        )
        assert connection.execute(
            "SELECT status,provider_status,fulfillment_status,processing_owner FROM payment WHERE id=3"
        ).fetchone() == ("pending", "succeeded", "retry", None)
        assert connection.execute("SELECT checkout_status FROM payment WHERE id=4").fetchone()[0] == "review"
        connection.execute("UPDATE payment SET provider_idempotency_key='unique' WHERE id=1")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE payment SET provider_idempotency_key='unique' WHERE id=2")


def test_migration_failure_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    from panel_core import db_migration

    database = str(tmp_path / "rollback.db")
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE retained (value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('original')")
        connection.execute("PRAGMA user_version=28")

    def fail(*args):
        raise RuntimeError("injected index failure")

    monkeypatch.setattr(db_migration, "_ensure_model_indexes", fail)
    with pytest.raises(RuntimeError, match="injected"):
        migrate_sqlite_db(database, seed_bot_texts=False)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 28
        assert connection.execute("SELECT value FROM retained").fetchone()[0] == "original"
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("retained",)]


def test_wireguard_backfill_is_persisted_and_keeps_disabled_peers(tmp_path):
    database = str(tmp_path / "wireguard.db")
    migrate_sqlite_db(database, seed_bot_texts=False)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO inbound (tag,port,protocol,stream_settings,device_limit) VALUES ('wg',1234,'wireguard','{}',0)"
        )
        connection.executemany(
            "INSERT INTO client (id,email,inbound_tag,enable) VALUES (?,?,'wg',?)",
            [("first", "one", 1), ("second", "two", 0)],
        )
        connection.execute("PRAGMA user_version=28")
    migrate_sqlite_db(database, seed_bot_texts=False)
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT id,wg_address FROM client ORDER BY id").fetchall()
        assert all(address and address.endswith("/32") for _, address in before)
        assert len({address for _, address in before}) == 2
        connection.execute("UPDATE client SET enable=1-enable")
    migrate_sqlite_db(database, seed_bot_texts=False)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id,wg_address FROM client ORDER BY id").fetchall() == before


def test_current_version_does_not_hide_missing_required_index(tmp_path):
    from panel_core.app_base import _require_schema

    database = str(tmp_path / "missing-index.db")
    migrate_sqlite_db(database, seed_bot_texts=False)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX ix_payment_last_checked_at")
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{database}"
    db.init_app(app)
    with app.app_context():
        with pytest.raises(RuntimeError, match="ix_payment_last_checked_at"):
            _require_schema()
        db.session.remove()
        db.engine.dispose()


@pytest.mark.skipif(not os.getenv("DATABASE_URL_TEST"), reason="isolated PostgreSQL DSN not configured")
def test_postgres_upgrade_executes_json_defaults_and_backfills_history():
    from sqlalchemy import text
    from panel_core.pg_migrate import migrate_postgres_db
    from panel_core.app_base import _require_schema

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL_TEST"]
    db.init_app(app)
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE"))
        db.session.execute(text("CREATE SCHEMA public"))
        db.session.commit()
        migrate_postgres_db()
        db.session.execute(
            text(
                "INSERT INTO provision_operation (id,source_id,telegram_id,source,snapshot) VALUES ('op','pay:1',42,'payment','{}')"
            )
        )
        db.session.execute(text("ALTER TABLE provision_operation DROP COLUMN params"))
        db.session.execute(
            text(
                "INSERT INTO payment (telegram_id,tariff_id,tariff_snapshot,amount_rub,yookassa_id,status) VALUES (42,1,'{}',100,'legacy-refund','refunded')"
            )
        )
        db.session.execute(text("UPDATE schema_version SET version=28"))
        db.session.execute(text("DROP INDEX ix_payment_last_checked_at"))
        db.session.commit()
        migrate_postgres_db()
        assert db.session.execute(text("SELECT params FROM provision_operation WHERE id='op'")).scalar() == {}
        assert db.session.execute(
            text(
                "SELECT provider_status,fulfillment_status,refund_status,refunded_amount_kopeks FROM payment WHERE yookassa_id='legacy-refund'"
            )
        ).one() == ("succeeded", "succeeded", "none", 0)
        _require_schema()
        db.session.remove()
        db.engine.dispose()
