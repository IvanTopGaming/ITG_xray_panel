"""Integration tests for the v9 → v10 migration."""

import os
import sqlite3
import tempfile

import pytest

from db_migration import _ensure_billing_tables


@pytest.fixture
def fresh_db():
    """Create a fresh SQLite file with just the basic schema (no migrations)."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    # Bootstrap a minimal `client` table so FK from notification_log resolves.
    # NOTE: client.id is VARCHAR(128) in production (UUID-like strings), so the
    # FK column in notification_log is also VARCHAR(128).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS client (
            id VARCHAR(128) PRIMARY KEY,
            email TEXT NOT NULL,
            inbound_tag TEXT NOT NULL
        )
    """)
    conn.commit()
    yield path, conn, cursor
    conn.close()
    os.unlink(path)


def test_ensure_billing_tables_creates_all_eight(fresh_db):
    path, conn, cursor = fresh_db
    _ensure_billing_tables(cursor)
    conn.commit()

    expected = {
        "tariff",
        "tariff_item",
        "user_tariff_access",
        "payment",
        "bot_text",
        "bot_event",
        "telegram_user",
        "notification_log",
    }
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    found = {row[0] for row in cursor.fetchall()}
    assert expected.issubset(found), f"Missing tables: {expected - found}"


def test_ensure_billing_tables_idempotent(fresh_db):
    path, conn, cursor = fresh_db
    _ensure_billing_tables(cursor)
    _ensure_billing_tables(cursor)  # second call must not error
    conn.commit()


def test_alter_client_billing_columns_adds_two_columns(fresh_db):
    from db_migration import _alter_client_billing_columns

    path, conn, cursor = fresh_db
    # baseline: client table has only id, email, inbound_tag (per fixture)
    cursor.execute("PRAGMA table_info(client)")
    before = {row[1] for row in cursor.fetchall()}
    assert "telegram_id" not in before
    assert "tariff_id" not in before

    _alter_client_billing_columns(cursor)
    conn.commit()

    cursor.execute("PRAGMA table_info(client)")
    after = {row[1] for row in cursor.fetchall()}
    assert "telegram_id" in after
    assert "tariff_id" in after


def test_alter_client_billing_columns_idempotent(fresh_db):
    from db_migration import _alter_client_billing_columns

    path, conn, cursor = fresh_db
    _alter_client_billing_columns(cursor)
    _alter_client_billing_columns(cursor)  # second call must not error
    conn.commit()


def test_seed_bot_texts_empty_yaml_no_op(fresh_db, tmp_path):
    from db_migration import _ensure_billing_tables, _seed_bot_texts

    path, conn, cursor = fresh_db
    _ensure_billing_tables(cursor)
    conn.commit()
    empty_yaml = tmp_path / "empty.yaml"
    empty_yaml.write_text("", encoding="utf-8")
    inserted = _seed_bot_texts(cursor, defaults_path=str(empty_yaml))
    conn.commit()
    assert inserted == 0
    cursor.execute("SELECT COUNT(*) FROM bot_text")
    assert cursor.fetchone()[0] == 0


def test_seed_bot_texts_inserts_new_keys(fresh_db, tmp_path):
    from db_migration import _ensure_billing_tables, _seed_bot_texts

    path, conn, cursor = fresh_db
    _ensure_billing_tables(cursor)
    conn.commit()

    yaml_content = """
welcome.title:
  ru: "Привет"
  en: "Hi"
menu.keys:
  ru: "Мои ключи"
  en: "My keys"
"""
    yaml_path = tmp_path / "defaults.yaml"
    yaml_path.write_text(yaml_content, encoding="utf-8")

    inserted = _seed_bot_texts(cursor, defaults_path=str(yaml_path))
    conn.commit()
    assert inserted == 4  # 2 keys × 2 langs
    cursor.execute("SELECT key, lang, text FROM bot_text ORDER BY key, lang")
    rows = cursor.fetchall()
    assert ("menu.keys", "en", "My keys") in rows
    assert ("welcome.title", "ru", "Привет") in rows


def test_seed_bot_texts_does_not_overwrite_existing(fresh_db, tmp_path):
    from db_migration import _ensure_billing_tables, _seed_bot_texts

    path, conn, cursor = fresh_db
    _ensure_billing_tables(cursor)
    conn.commit()
    cursor.execute(
        "INSERT INTO bot_text (key, lang, text) VALUES (?, ?, ?)",
        ("welcome.title", "ru", "CUSTOM ADMIN VALUE"),
    )
    conn.commit()

    yaml_path = tmp_path / "defaults.yaml"
    yaml_path.write_text(
        'welcome.title:\n  ru: "default"\n  en: "default"\n',
        encoding="utf-8",
    )
    inserted = _seed_bot_texts(cursor, defaults_path=str(yaml_path))
    conn.commit()
    assert inserted == 1  # only 'en' inserted; 'ru' was already there
    cursor.execute("SELECT text FROM bot_text WHERE key='welcome.title' AND lang='ru'")
    assert cursor.fetchone()[0] == "CUSTOM ADMIN VALUE"


def test_migrate_full_pipeline_to_v10(fresh_db):
    """Run migrate_sqlite_db on a fresh-ish v9 DB; expect current version + all tables."""
    from db_migration import CURRENT_DB_VERSION, migrate_sqlite_db

    path, conn, cursor = fresh_db
    # Simulate a v9 database
    cursor.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()  # migrate_sqlite_db opens its own connection

    migrate_sqlite_db(path)

    conn = sqlite3.connect(path)
    cursor = conn.cursor()

    # Version bumped to current
    cursor.execute("PRAGMA user_version")
    assert cursor.fetchone()[0] == CURRENT_DB_VERSION

    # All 8 billing tables exist
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    found = {row[0] for row in cursor.fetchall()}
    expected = {
        "tariff",
        "tariff_item",
        "user_tariff_access",
        "payment",
        "bot_text",
        "bot_event",
        "telegram_user",
        "notification_log",
    }
    assert expected.issubset(found)

    # Client gained the two new columns
    cursor.execute("PRAGMA table_info(client)")
    cols = {row[1] for row in cursor.fetchall()}
    assert "telegram_id" in cols
    assert "tariff_id" in cols

    conn.close()


def test_migrate_idempotent_on_v10(fresh_db):
    """Re-running migrate on an already-migrated DB is a no-op."""
    from db_migration import CURRENT_DB_VERSION, migrate_sqlite_db

    path, conn, cursor = fresh_db
    cursor.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()
    migrate_sqlite_db(path)
    # Second run — no errors
    migrate_sqlite_db(path)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA user_version")
    assert cursor.fetchone()[0] == CURRENT_DB_VERSION
    conn.close()


def test_alter_client_billing_columns_skips_when_client_table_absent():
    """Defensive: if client table doesn't exist yet (fresh DB before
    SQLAlchemy create_all), the helper must not crash."""
    from db_migration import _alter_client_billing_columns
    import tempfile
    import sqlite3

    fd, path = tempfile.mkstemp(suffix=".sqlite")
    import os as _os

    _os.close(fd)
    try:
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        # No client table at all
        result = _alter_client_billing_columns(cursor)
        assert result == 0
        conn.close()
    finally:
        _os.unlink(path)
