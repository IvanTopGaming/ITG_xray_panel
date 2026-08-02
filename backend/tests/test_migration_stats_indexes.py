import os
import sqlite3
import tempfile

import pytest

from panel_core.db_migration import _ensure_stats_indexes, _index_exists


@pytest.fixture
def stats_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE traffic_snapshot (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT    NOT NULL,
            entity_id   TEXT    NOT NULL,
            inbound_tag TEXT    NOT NULL DEFAULT '',
            bucket      INTEGER NOT NULL,
            up          INTEGER DEFAULT 0,
            down        INTEGER DEFAULT 0
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE domain_stat (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT    NOT NULL,
            domain       TEXT    NOT NULL,
            client_email TEXT    NOT NULL DEFAULT '',
            inbound_tag  TEXT    NOT NULL DEFAULT '',
            hit_count    INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    yield conn, cursor
    conn.close()
    os.unlink(path)


def test_composite_indexes_created_on_existing_tables(stats_db):
    conn, cursor = stats_db
    assert not _index_exists(cursor, "ix_ts_type_bucket")
    assert not _index_exists(cursor, "ix_ds_date_domain")

    created = _ensure_stats_indexes(cursor)
    conn.commit()

    assert created == 2
    assert _index_exists(cursor, "ix_ts_type_bucket")
    assert _index_exists(cursor, "ix_ds_date_domain")


def test_composite_indexes_cover_expected_columns(stats_db):
    conn, cursor = stats_db
    _ensure_stats_indexes(cursor)
    conn.commit()

    cursor.execute("PRAGMA index_info(ix_ts_type_bucket)")
    assert [row[2] for row in cursor.fetchall()] == ["entity_type", "bucket"]

    cursor.execute("PRAGMA index_info(ix_ds_date_domain)")
    assert [row[2] for row in cursor.fetchall()] == ["date", "domain"]


def test_idempotent(stats_db):
    conn, cursor = stats_db
    assert _ensure_stats_indexes(cursor) == 2
    conn.commit()
    assert _ensure_stats_indexes(cursor) == 0
    conn.commit()
    assert _index_exists(cursor, "ix_ts_type_bucket")
    assert _index_exists(cursor, "ix_ds_date_domain")


def test_missing_tables_are_skipped():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    try:
        assert _ensure_stats_indexes(cursor) == 0
    finally:
        conn.close()
        os.unlink(path)
