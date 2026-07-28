import os
import sqlite3
import tempfile

import pytest

from panel_core.db_migration import CURRENT_DB_VERSION, _ensure_schema_columns


@pytest.fixture
def outbound_pre_v21():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE outbound (
            id INTEGER PRIMARY KEY,
            tag VARCHAR(50) NOT NULL,
            protocol VARCHAR(20) NOT NULL DEFAULT 'freedom'
        )
        """
    )
    cursor.execute("INSERT INTO outbound (tag) VALUES ('direct')")
    conn.commit()
    yield conn, cursor
    conn.close()
    os.unlink(path)


def test_schema_patch_adds_egress_columns(outbound_pre_v21):
    conn, cursor = outbound_pre_v21
    cursor.execute("PRAGMA table_info(outbound)")
    assert "send_through" not in {r[1] for r in cursor.fetchall()}

    _ensure_schema_columns(cursor)
    conn.commit()

    cursor.execute("PRAGMA table_info(outbound)")
    cols = {r[1] for r in cursor.fetchall()}
    assert {"send_through", "public_ip", "gateway"} <= cols


def test_current_db_version_is_24():
    assert CURRENT_DB_VERSION == 24
