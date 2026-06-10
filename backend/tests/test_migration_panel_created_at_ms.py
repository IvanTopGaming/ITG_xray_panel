import os
import sqlite3
import tempfile

import pytest

from db_migration import _apply_data_fixups


@pytest.fixture
def linked_panel_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE linked_panel (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL UNIQUE,
            url              TEXT    NOT NULL,
            federation_token TEXT    NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'unknown',
            last_poll        BIGINT,
            last_error       TEXT,
            enable           BOOLEAN NOT NULL DEFAULT 1,
            created_at       BIGINT  NOT NULL
        )
        """
    )
    conn.commit()
    yield conn, cursor
    conn.close()
    os.unlink(path)


def _insert(cursor, name, created_at):
    cursor.execute(
        "INSERT INTO linked_panel (name, url, federation_token, created_at) VALUES (?, ?, ?, ?)",
        (name, "https://x.test", "tok", created_at),
    )


def _created_at(cursor, name):
    cursor.execute("SELECT created_at FROM linked_panel WHERE name = ?", (name,))
    return cursor.fetchone()[0]


def test_seconds_value_is_multiplied_to_ms(linked_panel_db):

    conn, cursor = linked_panel_db
    legacy_seconds = 1_748_500_000
    _insert(cursor, "old-gateway", legacy_seconds)
    conn.commit()

    _apply_data_fixups(cursor)
    conn.commit()

    assert _created_at(cursor, "old-gateway") == legacy_seconds * 1000


def test_ms_value_is_left_alone(linked_panel_db):

    conn, cursor = linked_panel_db
    current_ms = 1_748_500_000_000
    _insert(cursor, "new-gateway", current_ms)
    conn.commit()

    _apply_data_fixups(cursor)
    conn.commit()

    assert _created_at(cursor, "new-gateway") == current_ms


def test_idempotent_does_not_double_multiply(linked_panel_db):

    conn, cursor = linked_panel_db
    legacy_seconds = 1_748_500_000
    _insert(cursor, "twice", legacy_seconds)
    conn.commit()

    _apply_data_fixups(cursor)
    _apply_data_fixups(cursor)
    conn.commit()

    assert _created_at(cursor, "twice") == legacy_seconds * 1000


def test_mixed_rows_each_handled_independently(linked_panel_db):

    conn, cursor = linked_panel_db
    _insert(cursor, "legacy", 1_700_000_000)
    _insert(cursor, "modern", 1_748_500_000_000)
    conn.commit()

    _apply_data_fixups(cursor)
    conn.commit()

    assert _created_at(cursor, "legacy") == 1_700_000_000_000
    assert _created_at(cursor, "modern") == 1_748_500_000_000


def _insert_with_last_poll(cursor, name, last_poll):
    cursor.execute(
        "INSERT INTO linked_panel (name, url, federation_token, last_poll, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, "https://x.test", "tok", last_poll, 1_748_500_000_000),
    )


def _last_poll(cursor, name):
    cursor.execute("SELECT last_poll FROM linked_panel WHERE name = ?", (name,))
    return cursor.fetchone()[0]


def test_last_poll_seconds_value_is_multiplied_to_ms(linked_panel_db):

    conn, cursor = linked_panel_db
    _insert_with_last_poll(cursor, "old-last-poll", 1_748_500_000)
    conn.commit()

    _apply_data_fixups(cursor)
    conn.commit()

    assert _last_poll(cursor, "old-last-poll") == 1_748_500_000_000


def test_last_poll_ms_value_is_left_alone(linked_panel_db):
    conn, cursor = linked_panel_db
    _insert_with_last_poll(cursor, "new-last-poll", 1_748_500_000_000)
    conn.commit()

    _apply_data_fixups(cursor)
    conn.commit()

    assert _last_poll(cursor, "new-last-poll") == 1_748_500_000_000


def test_last_poll_null_is_left_alone(linked_panel_db):

    conn, cursor = linked_panel_db
    _insert_with_last_poll(cursor, "never-polled", None)
    conn.commit()

    _apply_data_fixups(cursor)
    conn.commit()

    assert _last_poll(cursor, "never-polled") is None
