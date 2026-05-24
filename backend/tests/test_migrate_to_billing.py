"""Unit tests for scripts/migrate_to_billing.py.

The script is standalone-sqlite3 (no Flask app context) and takes two paths:
the panel DB to mutate and the legacy bot DB to read from.
"""

import os
import sqlite3
import tempfile

import pytest

from db_migration import migrate_sqlite_db
from scripts.migrate_to_billing import migrate


@pytest.fixture
def panel_db_path():
    """Empty panel DB freshly migrated to current schema, with one inbound + two clients."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=9")
    conn.execute("CREATE TABLE client (id TEXT PRIMARY KEY, inbound_tag TEXT, email TEXT)")
    conn.execute(
        "CREATE TABLE inbound (id INTEGER PRIMARY KEY, tag TEXT, protocol TEXT, stream_settings TEXT, port INTEGER)"
    )
    conn.execute("CREATE TABLE system_setting (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO inbound VALUES (1, 'vless-de', 'vless', '{}', 443)")
    conn.execute("INSERT INTO client VALUES ('cli-1', 'vless-de', 'alice')")
    conn.execute("INSERT INTO client VALUES ('cli-2', 'vless-de', 'bob')")
    conn.commit()
    conn.close()
    migrate_sqlite_db(path)
    yield path
    os.unlink(path)


@pytest.fixture
def legacy_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE users (
            db_id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            username TEXT,
            panel_email TEXT NOT NULL,
            inbound_tag TEXT NOT NULL,
            uuid TEXT
        )
    """)
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


def _seed_legacy(path, rows):
    conn = sqlite3.connect(path)
    for telegram_id, username, email, uuid in rows:
        conn.execute(
            "INSERT INTO users (telegram_id, username, panel_email, inbound_tag, uuid) VALUES (?, ?, ?, ?, ?)",
            (telegram_id, username, email, "vless-de", uuid),
        )
    conn.commit()
    conn.close()


def _client(panel_db_path, client_id):
    conn = sqlite3.connect(panel_db_path)
    row = conn.execute("SELECT id, email, telegram_id, tariff_id FROM client WHERE id=?", (client_id,)).fetchone()
    conn.close()
    return row


def test_migrate_links_existing_clients(panel_db_path, legacy_db_path):
    _seed_legacy(
        legacy_db_path,
        [
            (42, "alice_tg", "alice", "cli-1"),
            (43, "bob_tg", "bob", "cli-2"),
        ],
    )
    result = migrate(panel_db_path, legacy_db_path)
    assert result["linked"] == 2
    assert result["telegram_users"] == 2
    assert result["orphaned"] == 0
    assert _client(panel_db_path, "cli-1")[2] == 42
    assert _client(panel_db_path, "cli-2")[2] == 43


def test_migrate_renames_singleton_to_canonical_email(panel_db_path, legacy_db_path):
    _seed_legacy(legacy_db_path, [(42, "alice", "alice", "cli-1")])
    migrate(panel_db_path, legacy_db_path)
    assert _client(panel_db_path, "cli-1")[1] == "tg42_vless-de"


def test_migrate_handles_family_share_with_double_underscore_suffix(panel_db_path, legacy_db_path):
    _seed_legacy(
        legacy_db_path,
        [
            (42, "alice", "alice", "cli-1"),
            (42, "alice", "bob", "cli-2"),
        ],
    )
    migrate(panel_db_path, legacy_db_path)
    assert _client(panel_db_path, "cli-1")[1] == "tg42_vless-de__alice"
    assert _client(panel_db_path, "cli-2")[1] == "tg42_vless-de__bob"


def test_migrate_records_orphans_when_no_client(panel_db_path, legacy_db_path):
    _seed_legacy(
        legacy_db_path,
        [
            (42, "alice", "alice", "cli-1"),
            (99, "missing", "ghost", "cli-missing"),
        ],
    )
    result = migrate(panel_db_path, legacy_db_path)
    assert result["linked"] == 1
    assert result["orphaned"] == 1
    assert _client(panel_db_path, "cli-1")[2] == 42


def test_migrate_does_not_touch_tariff_id(panel_db_path, legacy_db_path):
    _seed_legacy(legacy_db_path, [(42, "alice", "alice", "cli-1")])
    migrate(panel_db_path, legacy_db_path)
    assert _client(panel_db_path, "cli-1")[3] is None


def test_migrate_is_idempotent(panel_db_path, legacy_db_path):
    _seed_legacy(legacy_db_path, [(42, "alice", "alice", "cli-1")])
    migrate(panel_db_path, legacy_db_path)
    result2 = migrate(panel_db_path, legacy_db_path)
    assert result2["linked"] == 0
    assert result2["telegram_users"] == 0
    assert result2["orphaned"] == 0
    assert _client(panel_db_path, "cli-1")[2] == 42


def test_migrate_returns_zero_when_legacy_db_missing(panel_db_path):
    result = migrate(panel_db_path, "/tmp/this-does-not-exist-xyz.db")
    assert result == {"linked": 0, "telegram_users": 0, "orphaned": 0}
