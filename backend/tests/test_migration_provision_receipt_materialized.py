import sqlite3

from panel_core.db_migration import CURRENT_DB_VERSION, migrate_sqlite_db


def _columns(cursor, table):
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_database_gets_the_column(tmp_path):
    db_file = tmp_path / "panel.db"
    sqlite3.connect(db_file).close()

    migrate_sqlite_db(str(db_file))

    conn = sqlite3.connect(db_file)
    assert "materialized" in _columns(conn.cursor(), "provision_receipt")
    conn.close()


def test_existing_table_gains_the_column_and_old_rows_read_false(tmp_path):
    db_file = tmp_path / "panel.db"
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE provision_receipt (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT   NOT NULL,
            inbound_tag     TEXT   NOT NULL,
            telegram_id     BIGINT NOT NULL,
            response_json   TEXT   NOT NULL,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_provision_receipt UNIQUE (idempotency_key, inbound_tag)
        )
        """
    )
    cur.execute(
        "INSERT INTO provision_receipt (idempotency_key, inbound_tag, telegram_id, response_json) "
        "VALUES ('old', 'tag', 7, '{}')"
    )
    conn.commit()
    conn.close()

    migrate_sqlite_db(str(db_file))

    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    assert "materialized" in _columns(cur, "provision_receipt")
    assert cur.execute("SELECT materialized FROM provision_receipt WHERE idempotency_key = 'old'").fetchone()[0] == 0
    assert cur.execute("PRAGMA user_version").fetchone()[0] == CURRENT_DB_VERSION
    conn.close()
