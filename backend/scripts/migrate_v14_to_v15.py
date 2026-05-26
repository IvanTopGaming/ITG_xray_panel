#!/usr/bin/env python3
"""One-time migration: v14 (Node system) → v15 (Panel Federation system).

Run this BEFORE starting the new version of the panel.

Usage:
    python scripts/migrate_v14_to_v15.py /path/to/panel.db
"""

from __future__ import annotations

import sqlite3
import sys
from typing import List


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_version(cur: sqlite3.Cursor) -> int:
    cur.execute("PRAGMA user_version")
    row = cur.fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _table_exists(cur: sqlite3.Cursor, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def _table_columns(cur: sqlite3.Cursor, name: str) -> List[str]:
    if not _table_exists(cur, name):
        return []
    cur.execute(f"PRAGMA table_info({name})")
    return [str(row[1]) for row in cur.fetchall()]


def _recreate_without_columns(
    cur: sqlite3.Cursor,
    table: str,
    drop_cols: List[str],
) -> None:
    """Recreate *table* keeping all columns except those in *drop_cols*.

    Strategy (SQLite-safe):
      1. Get the current column list via PRAGMA table_info.
      2. Build 'CREATE TABLE new_<table> AS SELECT kept_cols FROM <table>'.
      3. DROP TABLE <table>.
      4. ALTER TABLE new_<table> RENAME TO <table>.
    """
    all_cols = _table_columns(cur, table)
    if not all_cols:
        print(f"  [SKIP] table '{table}' does not exist — nothing to recreate.")
        return

    drop_set = set(drop_cols)
    kept = [c for c in all_cols if c not in drop_set]
    missing = drop_set - set(all_cols)
    if missing:
        print(f"  [INFO] columns not present in '{table}' (already removed?): " + ", ".join(sorted(missing)))

    col_list = ", ".join(kept)
    tmp = f"_new_{table}"

    cur.execute(f"CREATE TABLE {tmp} AS SELECT {col_list} FROM {table}")
    cur.execute(f"DROP TABLE {table}")
    cur.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
    dropped = drop_set & set(all_cols)
    print(f"  Recreated '{table}': dropped [{', '.join(sorted(dropped))}], kept {len(kept)} columns.")


# ---------------------------------------------------------------------------
# Migration steps
# ---------------------------------------------------------------------------


def _step_check_version(cur: sqlite3.Cursor) -> None:
    ver = _get_version(cur)
    if ver != 14:
        print(
            f"ERROR: expected PRAGMA user_version == 14, found {ver}.\n"
            "This script only migrates from v14 to v15.  Aborting.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"  Version check passed: user_version = {ver}")


def _step_recreate_client(cur: sqlite3.Cursor) -> None:
    """Drop global_limit_bytes and allowed_node_groups from client."""
    _recreate_without_columns(cur, "client", ["global_limit_bytes", "allowed_node_groups"])
    # Recreate indexes that were dropped along with the table.
    cur.execute("CREATE INDEX IF NOT EXISTS ix_client_telegram ON client(telegram_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_client_tariff ON client(tariff_id)")
    print("  Recreated indexes on 'client'.")


def _step_recreate_inbound(cur: sqlite3.Cursor) -> None:
    """Drop master_disabled from inbound."""
    _recreate_without_columns(cur, "inbound", ["master_disabled"])


def _step_recreate_tariff_item(cur: sqlite3.Cursor) -> None:
    """Drop allowed_node_groups from tariff_item."""
    _recreate_without_columns(cur, "tariff_item", ["allowed_node_groups"])
    # Recreate index that was dropped along with the table.
    cur.execute("CREATE INDEX IF NOT EXISTS ix_tariff_item_tariff ON tariff_item(tariff_id)")
    print("  Recreated indexes on 'tariff_item'.")


def _step_drop_node_tables(cur: sqlite3.Cursor) -> None:
    """Drop node_client_traffic and node tables."""
    cur.execute("DROP TABLE IF EXISTS node_client_traffic")
    print("  Dropped table 'node_client_traffic' (if existed).")
    cur.execute("DROP TABLE IF EXISTS node")
    print("  Dropped table 'node' (if existed).")


def _step_delete_master_groups_setting(cur: sqlite3.Cursor) -> None:
    cur.execute("DELETE FROM system_setting WHERE key = 'master_groups'")
    print(f"  Deleted 'master_groups' from system_setting ({cur.rowcount} row(s) removed).")


def _step_create_linked_panel(cur: sqlite3.Cursor) -> None:
    if _table_exists(cur, "linked_panel"):
        print("  Table 'linked_panel' already exists — skipping creation.")
        return
    cur.execute(
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
    print("  Created table 'linked_panel'.")


def _step_create_federation_config(cur: sqlite3.Cursor) -> None:
    if _table_exists(cur, "federation_config"):
        print("  Table 'federation_config' already exists — skipping creation.")
    else:
        cur.execute(
            """
            CREATE TABLE federation_config (
                id                INTEGER PRIMARY KEY CHECK (id = 1),
                master_url        TEXT,
                master_name       TEXT,
                federation_token  TEXT,
                link_token        TEXT,
                link_token_used   BOOLEAN NOT NULL DEFAULT 0,
                linked_at         BIGINT
            )
            """
        )
        print("  Created table 'federation_config'.")

    cur.execute("INSERT OR IGNORE INTO federation_config (id) VALUES (1)")
    if cur.rowcount:
        print("  Inserted singleton row into 'federation_config'.")
    else:
        print("  Singleton row in 'federation_config' already present — skipped INSERT.")


def _step_add_tariff_item_panel_id(cur: sqlite3.Cursor) -> None:
    """Add panel_id FK column to tariff_item (after recreate)."""
    cols = _table_columns(cur, "tariff_item")
    if "panel_id" in cols:
        print("  Column 'tariff_item.panel_id' already exists — skipping ALTER.")
        return
    cur.execute("ALTER TABLE tariff_item ADD COLUMN panel_id INTEGER REFERENCES linked_panel(id)")
    print("  Added column 'tariff_item.panel_id'.")


def _step_set_version(cur: sqlite3.Cursor) -> None:
    cur.execute("PRAGMA user_version = 15")
    print("  Set PRAGMA user_version = 15.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()

        print("\n[1/9]  Checking database version …")
        _step_check_version(cur)

        print("\n[2/9]  Recreating 'client' table (drop global_limit_bytes, allowed_node_groups) …")
        _step_recreate_client(cur)

        print("\n[3/9]  Recreating 'inbound' table (drop master_disabled) …")
        _step_recreate_inbound(cur)

        print("\n[4/9]  Recreating 'tariff_item' table (drop allowed_node_groups) …")
        _step_recreate_tariff_item(cur)

        print("\n[5/9]  Dropping legacy node tables …")
        _step_drop_node_tables(cur)

        print("\n[6/9]  Removing 'master_groups' system setting …")
        _step_delete_master_groups_setting(cur)

        print("\n[7/9]  Creating 'linked_panel' table …")
        _step_create_linked_panel(cur)

        print("\n[8/9]  Creating 'federation_config' table and seeding singleton …")
        _step_create_federation_config(cur)

        print("\n[8b]   Adding 'tariff_item.panel_id' column …")
        _step_add_tariff_item_panel_id(cur)

        print("\n[9/9]  Bumping schema version to 15 …")
        _step_set_version(cur)

        conn.commit()
        print("\nMigration complete: v14 → v15.\n")

    except Exception as exc:
        conn.rollback()
        print(f"\nERROR — rolled back: {exc}", file=sys.stderr)
        raise
    finally:
        conn.close()


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)

    db_path = sys.argv[1]
    print("WARNING: Back up your database before running this.")
    migrate(db_path)


if __name__ == "__main__":
    main()
