"""Schema 25: one device ledger keyed by the Telegram account, two retired tables gone.

The shape of this change was dictated by §40 — `migrate_postgres_db` was `create_all()` plus a
few statements and owned no `ALTER TABLE`, so a **new column** on an existing table reached a
live Postgres never, while a **new table** arrived on its own. The ledger is therefore a table
(`user_device`), exactly as wave 3a's idempotency key was, and not a column on `client_device`
— which would additionally have needed `client_id` to lose its NOT NULL and a new unique key,
neither of which any migration here could deliver.

**Wave 9 lifted the column half of that constraint** (`_add_missing_columns`), so the reasoning
above is history rather than a live limit. The shape stays: a ledger keyed by the account is the
right one on its own merits, and `client_id` must not come back — the sub role holds no `Client`
row for a node-issued client, so a ledger joined through one counts zero.

The Postgres tests below are the ones that matter for that reasoning: they migrate a database
where the retired tables **already exist**, which is the case SQLite cannot reproduce (there
`_add_column_if_missing` and a table rebuild are both available).
"""

import os
import sqlite3

import pytest

from panel_core.db_migration import CURRENT_DB_VERSION, RETIRED_TABLES, migrate_sqlite_db


def test_current_db_version_is_27():
    assert CURRENT_DB_VERSION == 27, (
        "27 adds user_tariff_access.access_until -- the grant's own end date. Bumping the number "
        f"without adding a matching schema patch leaves live databases behind; got {CURRENT_DB_VERSION}"
    )


def test_the_ledger_does_not_depend_on_a_client_row():
    from panel_core.extensions import db

    import panel_core.models  # noqa: F401

    assert "user_device" in db.metadata.tables
    columns = db.metadata.tables["user_device"].columns
    assert "telegram_id" in columns
    assert "client_id" not in columns, (
        "the ledger must not depend on a Client row: the sub role holds none for node-issued clients"
    )


def test_the_retired_tables_are_not_models_any_more():
    from panel_core.extensions import db

    import panel_core.models  # noqa: F401

    for table in RETIRED_TABLES:
        assert table not in db.metadata.tables


def test_sqlite_creates_the_ledger(tmp_path):
    db_path = str(tmp_path / "panel.db")
    migrate_sqlite_db(db_path)

    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(user_device)").fetchall()}
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    conn.close()

    assert {"telegram_id", "hwid", "first_seen", "last_seen", "hits"}.issubset(cols)
    assert "ix_user_device_telegram_id" in indexes


def test_sqlite_enforces_one_row_per_account_and_hwid(tmp_path):
    db_path = str(tmp_path / "panel.db")
    migrate_sqlite_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO user_device (telegram_id, hwid, first_seen, last_seen) VALUES (1, 'hw', 0, 0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO user_device (telegram_id, hwid, first_seen, last_seen) VALUES (1, 'hw', 0, 0)")
    conn.execute("INSERT INTO user_device (telegram_id, hwid, first_seen, last_seen) VALUES (2, 'hw', 0, 0)")
    conn.commit()
    conn.close()


def test_sqlite_drops_the_retired_tables_it_used_to_create(tmp_path):
    """A node upgrading in place: both tables exist and one of them holds rows."""

    db_path = str(tmp_path / "panel.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE node_traffic_snapshot (id INTEGER PRIMARY KEY, panel_id INTEGER)")
    conn.execute("CREATE TABLE client_device (id INTEGER PRIMARY KEY, client_id TEXT, hwid TEXT)")
    conn.execute("INSERT INTO client_device (client_id, hwid) VALUES ('c1', 'hw')")
    conn.commit()
    conn.close()

    report = migrate_sqlite_db(db_path)
    assert report["retired_tables_dropped"] == 2

    conn = sqlite3.connect(db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert not names & set(RETIRED_TABLES)


def test_the_postgres_path_actually_drops_the_retired_tables():
    """Without Postgres this still pins the wiring: create_all alone drops nothing."""

    import inspect as py_inspect

    from panel_core import pg_migrate

    source = py_inspect.getsource(pg_migrate.migrate_postgres_db)
    assert "_drop_retired_tables()" in source, (
        "create_all() only adds tables. A retired one survives forever unless it is dropped explicitly."
    )


DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pg_only = pytest.mark.skipif(not DSN, reason="DATABASE_URL_TEST not set")


def _pg_app():
    from flask import Flask

    from panel_core.extensions import db

    import panel_core.models  # noqa: F401

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = DSN
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app, db


@pg_only
def test_postgres_delivers_the_ledger_to_a_database_that_already_had_the_old_one():
    """The §40 case: a live database, not a virgin one.

    The old `client_device` and `node_traffic_snapshot` are created by hand first, so the
    migration runs against a schema that already exists — which is where a column-shaped change
    would have failed silently and `UndefinedColumn` would have surfaced on the first query.
    """

    from sqlalchemy import inspect, text

    from panel_core.pg_migrate import migrate_postgres_db

    app, db = _pg_app()
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.execute(
            text(
                "CREATE TABLE client_device (id serial primary key, client_id varchar(128) NOT NULL, "
                "hwid varchar(128) NOT NULL, first_seen bigint NOT NULL, last_seen bigint NOT NULL)"
            )
        )
        db.session.execute(
            text("CREATE TABLE node_traffic_snapshot (id serial primary key, panel_id integer NOT NULL)")
        )
        db.session.execute(
            text("INSERT INTO client_device (client_id, hwid, first_seen, last_seen) VALUES ('c', 'h', 0, 0)")
        )
        db.session.commit()

        report = migrate_postgres_db()
        assert report["retired_tables_dropped"] == 2

        tables = set(inspect(db.engine).get_table_names())
        assert "user_device" in tables
        assert not tables & set(RETIRED_TABLES)

        columns = {c["name"] for c in inspect(db.engine).get_columns("user_device")}
        assert "telegram_id" in columns

        db.session.execute(
            text("INSERT INTO user_device (telegram_id, hwid, first_seen, last_seen) VALUES (7, 'h', 0, 0)")
        )
        db.session.commit()
        assert db.session.execute(text("SELECT count(*) FROM user_device WHERE telegram_id = 7")).scalar() == 1


@pg_only
def test_postgres_migration_is_idempotent_once_the_tables_are_already_gone():
    from sqlalchemy import text

    from panel_core.pg_migrate import migrate_postgres_db

    app, db = _pg_app()
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()

        assert migrate_postgres_db()["retired_tables_dropped"] == 0
        assert migrate_postgres_db()["retired_tables_dropped"] == 0
