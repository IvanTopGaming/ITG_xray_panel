import os
import sqlite3
import tempfile

import pytest

from panel_core.db_migration import CURRENT_DB_VERSION, _ensure_schema_columns


@pytest.fixture
def payment_pre_v13():

    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE payment (
            id INTEGER PRIMARY KEY,
            yookassa_id VARCHAR(64) UNIQUE NOT NULL,
            telegram_id BIGINT NOT NULL,
            tariff_id INTEGER NOT NULL,
            tariff_snapshot JSON NOT NULL,
            amount_rub INTEGER NOT NULL,
            status VARCHAR(16) NOT NULL,
            confirmation_url TEXT,
            metadata JSON NOT NULL DEFAULT '{}',
            created_at DATETIME,
            paid_at DATETIME
        )
        """
    )
    cursor.execute(
        "INSERT INTO payment (yookassa_id, telegram_id, tariff_id, tariff_snapshot, amount_rub, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("yk-test-1", 1, 1, "{}", 199, "pending"),
    )
    conn.commit()
    yield conn, cursor
    conn.close()
    os.unlink(path)


def test_schema_patch_adds_chat_id_and_message_id(payment_pre_v13):
    conn, cursor = payment_pre_v13

    cursor.execute("PRAGMA table_info(payment)")
    before = {row[1] for row in cursor.fetchall()}
    assert "chat_id" not in before
    assert "message_id" not in before

    _ensure_schema_columns(cursor)
    conn.commit()

    cursor.execute("PRAGMA table_info(payment)")
    after = {row[1] for row in cursor.fetchall()}
    assert "chat_id" in after
    assert "message_id" in after

    cursor.execute("SELECT chat_id, message_id FROM payment WHERE yookassa_id='yk-test-1'")
    chat, msg = cursor.fetchone()
    assert chat is None
    assert msg is None


def test_schema_patch_idempotent(payment_pre_v13):
    conn, cursor = payment_pre_v13
    _ensure_schema_columns(cursor)
    _ensure_schema_columns(cursor)
    conn.commit()


def test_current_db_version_bumped():
    assert CURRENT_DB_VERSION >= 13
