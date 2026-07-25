import sqlite3

from panel_core.db_migration import CURRENT_DB_VERSION, migrate_sqlite_db


def _columns(cursor, table):
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}


def test_migration_creates_notification_claim_table(tmp_path):
    db_file = tmp_path / "panel.db"
    sqlite3.connect(db_file).close()

    migrate_sqlite_db(str(db_file))

    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    assert _columns(cur, "notification_claim") == {
        "id",
        "telegram_id",
        "tariff_id",
        "scope",
        "kind",
        "created_at",
    }
    conn.close()


def test_migration_claim_unique_constraint_rejects_duplicate(tmp_path):
    db_file = tmp_path / "panel.db"
    sqlite3.connect(db_file).close()
    migrate_sqlite_db(str(db_file))

    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("INSERT INTO notification_claim (telegram_id, tariff_id, scope, kind) VALUES (42, 7, '', 'expiry_1d')")
    conn.commit()
    try:
        cur.execute(
            "INSERT INTO notification_claim (telegram_id, tariff_id, scope, kind) VALUES (42, 7, '', 'expiry_1d')"
        )
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    conn.close()
    assert raised, "unique constraint must reject the duplicate claim"


def test_schema_version_is_bumped(tmp_path):
    db_file = tmp_path / "panel.db"
    sqlite3.connect(db_file).close()
    migrate_sqlite_db(str(db_file))

    conn = sqlite3.connect(db_file)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == CURRENT_DB_VERSION
    assert CURRENT_DB_VERSION >= 23
