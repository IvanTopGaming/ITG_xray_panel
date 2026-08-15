"""The grant carries its own end date, and a live database grows the column without being rebuilt.

A free grant was open-ended in effect -- the cron re-provisioned it forever -- but it was expressed
as a dated key renewed after it had already lapsed, which cost the holder an expiry warning every
cycle and up to fifteen minutes without access. The date moves into the grant so the key can say
what is true.

NULL is the open-ended value rather than a sentinel date: a far-future timestamp would still be a
date, and every reader would have to know which one meant "never".
"""

from __future__ import annotations

import sqlite3

from panel_core.db_migration import CURRENT_DB_VERSION, migrate_sqlite_db


def test_access_until_is_added_to_an_existing_grant_table(tmp_path):
    db_path = tmp_path / "db" / "panel.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE user_tariff_access ("
        " id INTEGER PRIMARY KEY, telegram_id BIGINT NOT NULL, tariff_id INTEGER NOT NULL,"
        " billing VARCHAR(8) NOT NULL, next_renewal_at TIMESTAMP, note VARCHAR(255), created_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO user_tariff_access (telegram_id, tariff_id, billing, next_renewal_at)"
        " VALUES (7, 1, 'free', '2026-09-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    migrate_sqlite_db(str(db_path), seed_bot_texts=False)

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(user_tariff_access)")}
    assert "access_until" in columns, (
        "an existing installation must grow the column in place -- the grant table holds live rows "
        f"and is never rebuilt; got {sorted(columns)}"
    )

    value = conn.execute("SELECT access_until FROM user_tariff_access WHERE telegram_id = 7").fetchone()[0]
    assert value is None, (
        "an existing grant must land open-ended, not dated: it was already being renewed forever, "
        f"so a date here would take access away from somebody who had it; got {value!r}"
    )

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == CURRENT_DB_VERSION, f"migration must record its version; got {version}"
    conn.close()
