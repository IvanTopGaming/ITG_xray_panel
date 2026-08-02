"""Turn one 2.x monolith database into a master's Postgres and one node's SQLite.

The 2.x master is a monolith: its own Xray, its own inbounds and clients, and at the same time every
tariff, payment and Telegram user in the deployment. The split architecture has no role like that --
a master cannot run Xray, and a node cannot reach the shared Postgres -- so the machine keeps serving
its keys as a node while everything else moves to the master's database.

Three rules decide where a table goes:

* what the node needs to keep serving traffic goes to the node -- inbounds, clients, outbounds,
  routing, its own federation token, its traffic history;
* what more than one host has to see goes to Postgres -- users, tariffs, grants, payments, panels;
* `system_setting` is split by row rather than by name. It holds `bot_token`,
  `yookassa_secret_key` and `bot_service_token` in clear text next to the three keys a node actually
  reads, and a node is the least trusted machine in the deployment.

Nothing here writes to the source database: it is opened read-only, and both outputs are new.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, func, insert, select, text

from panel_core.extensions import db
import panel_core.models  # noqa: F401

logger = logging.getLogger(__name__)

REQUIRED_SCHEMA_VERSION = 26

LEGACY_TABLES = {"client_device", "node_traffic_snapshot"}

SHARED_TABLES = {"admin", "system_setting"}

NODE_TABLES = {
    "admin",
    "system_setting",
    "inbound",
    "client",
    "outbound",
    "routing_profile",
    "balancer",
    "federation_config",
    "traffic_snapshot",
    "domain_stat",
    "notification_log",
    "provision_receipt",
}

MASTER_TABLES = {
    "admin",
    "system_setting",
    "telegram_user",
    "tariff",
    "tariff_item",
    "user_tariff_access",
    "payment",
    "bot_text",
    "bot_event",
    "notification_claim",
    "user_device",
    "linked_panel",
}

DROPPED_TABLES: set[str] = set()

NODE_SETTING_KEYS = ("xray_log_level", "geoip_url", "geosite_url")


class MigrationRefused(RuntimeError):
    pass


def _schema_version(sqlite_path):
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def require_migrated(sqlite_path):
    version = _schema_version(sqlite_path)
    if version < REQUIRED_SCHEMA_VERSION:
        raise MigrationRefused(
            f"{sqlite_path} is at schema {version}, and this split needs {REQUIRED_SCHEMA_VERSION}. "
            f"Run `PANEL_DB_PATH={sqlite_path} uv run python migrate_db.py` first: the 2.x schema "
            f"lacks columns the models already know about, and copying by the current model "
            f"definitions would fail halfway through on a SELECT."
        )
    return version


def write_node_settings(source_sqlite, target_sqlite):
    """Copy only the Xray-facing rows of system_setting into the node's database."""

    src = sqlite3.connect(f"file:{source_sqlite}?mode=ro", uri=True)
    dst = sqlite3.connect(target_sqlite)
    try:
        dst.execute("CREATE TABLE IF NOT EXISTS system_setting (key TEXT PRIMARY KEY, value TEXT)")
        placeholders = ",".join("?" for _ in NODE_SETTING_KEYS)
        rows = src.execute(
            f"SELECT key, value FROM system_setting WHERE key IN ({placeholders})",
            NODE_SETTING_KEYS,
        ).fetchall()
        dst.executemany("INSERT OR REPLACE INTO system_setting (key, value) VALUES (?, ?)", rows)
        dst.commit()
        return len(rows)
    finally:
        src.close()
        dst.close()


def _tables_for(names):
    return [t for t in db.metadata.sorted_tables if t.name in names]


def _copy(source_engine, target_conn, tables, where=None):
    counts = {}
    with source_engine.connect() as sconn:
        for table in tables:
            stmt = select(table)
            if where and table.name in where:
                stmt = stmt.where(where[table.name])
            rows = [dict(r._mapping) for r in sconn.execute(stmt)]
            if rows:
                target_conn.execute(insert(table), rows)
            counts[table.name] = len(rows)
    return counts


def _drop_foreign_keys(engine):
    with engine.begin() as conn:
        for tbl, name in conn.execute(
            text("SELECT conrelid::regclass AS tbl, conname FROM pg_constraint WHERE contype = 'f'")
        ).fetchall():
            conn.execute(text(f'ALTER TABLE {tbl} DROP CONSTRAINT IF EXISTS "{name}"'))


def _reset_sequences(conn):
    for table in db.metadata.sorted_tables:
        for col in table.primary_key.columns:
            seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table.name, "c": col.name}).scalar()
            if not seq:
                continue
            top = conn.execute(text(f"SELECT max({col.name}) FROM {table.name}")).scalar()
            if top is not None:
                conn.execute(text("SELECT setval(:s, :m)"), {"s": seq, "m": top})


def _refuse_if_populated(conn, tables):
    for table in tables:
        n = conn.execute(select(func.count()).select_from(table)).scalar()
        if n:
            raise MigrationRefused(
                f"target table {table.name} already holds {n} rows. This split is meant for a fresh "
                f"data tier; pass force=True only if you are certain you are re-running it."
            )


def split(source_sqlite, pg_url, node_sqlite, force=False):
    require_migrated(source_sqlite)

    source = create_engine(f"sqlite:///{source_sqlite}")
    report = {}

    node_path = Path(node_sqlite)
    if node_path.exists() and not force:
        raise MigrationRefused(f"{node_sqlite} already exists; refusing to overwrite it")
    node_path.parent.mkdir(parents=True, exist_ok=True)
    node_engine = create_engine(f"sqlite:///{node_sqlite}")
    db.metadata.create_all(node_engine)

    with node_engine.begin() as nconn:
        node_tables = _tables_for(NODE_TABLES - {"system_setting"})
        report["node"] = _copy(source, nconn, node_tables)
    node_engine.dispose()
    report["node"]["system_setting"] = write_node_settings(source_sqlite, node_sqlite)

    target = create_engine(pg_url)
    db.metadata.create_all(target)
    _drop_foreign_keys(target)
    master_tables = _tables_for(MASTER_TABLES)
    with target.begin() as tconn:
        if not force:
            _refuse_if_populated(tconn, master_tables)
        report["master"] = _copy(source, tconn, master_tables)
        _reset_sequences(tconn)
    target.dispose()
    source.dispose()

    with sqlite3.connect(node_sqlite) as con:
        con.execute(f"PRAGMA user_version = {REQUIRED_SCHEMA_VERSION}")

    return report


def checkpoint_source(sqlite_path, into):
    """Take a consistent copy of a live monolith, WAL included.

    The production master had 11 MB of unmerged WAL beside its 84 MB database. Copying panel.db on
    its own loses whatever is in there -- the most recent payments and grants, precisely the rows
    somebody will notice.
    """

    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(into)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return into


def copy_for_migration(sqlite_path, workdir):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / "monolith.db"
    return checkpoint_source(sqlite_path, str(target))
