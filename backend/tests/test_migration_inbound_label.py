"""Migration test: inbound.label column added by _ensure_schema_columns."""

import os
import sqlite3
import tempfile

import pytest

from db_migration import CURRENT_DB_VERSION, _ensure_schema_columns


@pytest.fixture
def inbound_pre_v12():
    """Fresh SQLite with a v11-shaped inbound table (no label)."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE inbound (
            id INTEGER PRIMARY KEY,
            tag VARCHAR(50) UNIQUE NOT NULL,
            port INTEGER UNIQUE NOT NULL,
            protocol VARCHAR(20) DEFAULT 'vless',
            stream_settings TEXT NOT NULL,
            routing_profile_id INTEGER,
            up BIGINT DEFAULT 0,
            down BIGINT DEFAULT 0,
            fallback_address VARCHAR(100),
            device_limit INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cursor.execute(
        "INSERT INTO inbound (tag, port, stream_settings) VALUES (?, ?, ?)",
        ("demo-tag", 12345, "{}"),
    )
    conn.commit()
    yield conn, cursor
    conn.close()
    os.unlink(path)


def test_schema_patch_adds_label(inbound_pre_v12):
    conn, cursor = inbound_pre_v12

    cursor.execute("PRAGMA table_info(inbound)")
    before = {row[1] for row in cursor.fetchall()}
    assert "label" not in before

    _ensure_schema_columns(cursor)
    conn.commit()

    cursor.execute("PRAGMA table_info(inbound)")
    after = {row[1] for row in cursor.fetchall()}
    assert "label" in after

    # Existing row defaults to NULL.
    cursor.execute("SELECT label FROM inbound WHERE tag = 'demo-tag'")
    assert cursor.fetchone()[0] is None


def test_schema_patch_idempotent(inbound_pre_v12):
    conn, cursor = inbound_pre_v12
    _ensure_schema_columns(cursor)
    _ensure_schema_columns(cursor)
    conn.commit()


def test_current_db_version_bumped():
    assert CURRENT_DB_VERSION >= 13
