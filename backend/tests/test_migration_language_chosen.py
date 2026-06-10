import os
import sqlite3
import tempfile

import pytest

from db_migration import CURRENT_DB_VERSION, _ensure_schema_columns


@pytest.fixture
def telegram_user_pre_v11():

    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE telegram_user (
            telegram_id BIGINT PRIMARY KEY,
            username VARCHAR(64),
            language VARCHAR(8) NOT NULL DEFAULT 'ru',
            trial_used_at DATETIME,
            blocked BOOLEAN NOT NULL DEFAULT 0,
            first_seen_at DATETIME,
            last_seen_at DATETIME,
            note VARCHAR(255)
        )
        """
    )
    cursor.execute("INSERT INTO telegram_user (telegram_id, language) VALUES (999, 'en')")
    conn.commit()
    yield conn, cursor
    conn.close()
    os.unlink(path)


def test_schema_patch_adds_language_chosen(telegram_user_pre_v11):
    conn, cursor = telegram_user_pre_v11

    cursor.execute("PRAGMA table_info(telegram_user)")
    before = {row[1] for row in cursor.fetchall()}
    assert "language_chosen" not in before

    _ensure_schema_columns(cursor)
    conn.commit()

    cursor.execute("PRAGMA table_info(telegram_user)")
    after = {row[1] for row in cursor.fetchall()}
    assert "language_chosen" in after

    cursor.execute("SELECT language_chosen FROM telegram_user WHERE telegram_id = 999")
    assert cursor.fetchone()[0] == 0


def test_schema_patch_idempotent(telegram_user_pre_v11):
    conn, cursor = telegram_user_pre_v11
    _ensure_schema_columns(cursor)
    _ensure_schema_columns(cursor)
    conn.commit()


def test_current_db_version_bumped():
    assert CURRENT_DB_VERSION >= 13
