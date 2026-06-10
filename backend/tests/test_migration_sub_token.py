import os
import sqlite3
import tempfile

import pytest

from db_migration import (
    CURRENT_DB_VERSION,
    _ensure_schema_columns,
    _backfill_sub_tokens,
)


@pytest.fixture
def telegram_user_pre_v18():

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
            language_chosen BOOLEAN NOT NULL DEFAULT 0,
            blocked BOOLEAN NOT NULL DEFAULT 0
        )
        """
    )
    cursor.execute("INSERT INTO telegram_user (telegram_id) VALUES (111)")
    cursor.execute("INSERT INTO telegram_user (telegram_id) VALUES (222)")
    conn.commit()
    yield conn, cursor
    conn.close()
    os.unlink(path)


def test_schema_patch_adds_sub_token(telegram_user_pre_v18):
    conn, cursor = telegram_user_pre_v18
    cursor.execute("PRAGMA table_info(telegram_user)")
    assert "sub_token" not in {r[1] for r in cursor.fetchall()}

    _ensure_schema_columns(cursor)
    conn.commit()

    cursor.execute("PRAGMA table_info(telegram_user)")
    assert "sub_token" in {r[1] for r in cursor.fetchall()}


def test_backfill_populates_unique_tokens(telegram_user_pre_v18):
    conn, cursor = telegram_user_pre_v18
    _ensure_schema_columns(cursor)
    n = _backfill_sub_tokens(cursor)
    conn.commit()

    assert n == 2
    cursor.execute("SELECT sub_token FROM telegram_user ORDER BY telegram_id")
    tokens = [r[0] for r in cursor.fetchall()]
    assert all(t and len(t) == 36 for t in tokens)
    assert len(set(tokens)) == 2


def test_backfill_idempotent(telegram_user_pre_v18):
    conn, cursor = telegram_user_pre_v18
    _ensure_schema_columns(cursor)
    _backfill_sub_tokens(cursor)
    conn.commit()
    cursor.execute("SELECT sub_token FROM telegram_user WHERE telegram_id = 111")
    first = cursor.fetchone()[0]

    second_run = _backfill_sub_tokens(cursor)
    conn.commit()
    assert second_run == 0
    cursor.execute("SELECT sub_token FROM telegram_user WHERE telegram_id = 111")
    assert cursor.fetchone()[0] == first


def test_unique_index_present(telegram_user_pre_v18):
    conn, cursor = telegram_user_pre_v18
    _ensure_schema_columns(cursor)
    _backfill_sub_tokens(cursor)
    conn.commit()
    cursor.execute("PRAGMA index_list(telegram_user)")
    idx_names = {r[1] for r in cursor.fetchall()}
    assert "ix_telegram_user_sub_token" in idx_names


def test_current_db_version_is_19():
    assert CURRENT_DB_VERSION == 19
