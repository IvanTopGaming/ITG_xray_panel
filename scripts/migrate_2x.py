"""Move a 2.x monolith's data into the split deployment. One file, one run, pip-installable deps.

    pip install 'sqlalchemy>=2,<3' psycopg2-binary
    python3 migrate_2x.py --sqlite panel.db --pg 'postgresql+psycopg2://...' [--clean-node]

It does exactly two things, and neither of them needs the panel's own code:

    --pg          copy the master-side tables out of the monolith into the shared Postgres
    --clean-node  delete those same tables from the SQLite the machine keeps as a node

Everything else is done by the product itself. The node migrates its own schema on start-up, and
the cron service owns the shared Postgres schema -- so by the time this runs, both databases already
have the right shape and this only moves rows.

Run it against a copy, never the live file: --sqlite is opened read-only for the Postgres half.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

MASTER_TABLES = [
    "admin",
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
    "system_setting",
]

# The cron service seeds these while it creates the schema -- 159 bot_text rows, the
# bot_texts_seeded_version setting -- so they are never empty on a correctly deployed data tier.
# Copying them would collide; refusing on them would refuse every real migration. They are merged
# on their natural key instead, and the merge is narrowed further below.
MERGE_TABLES = {
    "bot_text": ("key", "lang"),
    "system_setting": ("key",),
    "admin": ("username",),
}

# Only the texts an admin actually edited. The rest are defaults, and the ones the cron service
# just seeded are newer than the monolith's copy.
BOT_TEXT_MERGE_FILTER = "customized"

# Written by the schema machinery, not by the panel: carrying the monolith's value across would
# tell the new deployment its texts are older or newer than they are.
SETTING_KEYS_NOT_CARRIED = ("bot_texts_seeded_version",)

NODE_ONLY_TABLES = [
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
]

NODE_SETTING_KEYS = ("xray_log_level", "geoip_url", "geosite_url")

DROP_FROM_NODE = [
    "payment",
    "telegram_user",
    "user_tariff_access",
    "tariff_item",
    "tariff",
    "bot_text",
    "notification_claim",
    "user_device",
    "linked_panel",
]

REQUIRED_SCHEMA = 26


def fail(message, hint=""):
    print(f"\n  error: {message}\n", file=sys.stderr)
    if hint:
        print(f"    {hint}\n", file=sys.stderr)
    raise SystemExit(1)


def schema_version(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def checkpoint(source, target):
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def copy_to_postgres(sqlite_path, pg_url):
    try:
        from sqlalchemy import MetaData, create_engine, insert, select
    except ImportError:
        fail(
            "SQLAlchemy is not installed",
            "pip install 'sqlalchemy>=2,<3' psycopg2-binary",
        )

    version = schema_version(sqlite_path)
    if version < REQUIRED_SCHEMA:
        fail(
            f"the source is at schema {version}, this needs {REQUIRED_SCHEMA}",
            "Start the node with this database first -- it migrates its own schema on boot -- "
            "then run this against the migrated file.",
        )

    src = create_engine(f"sqlite:///{sqlite_path}")
    dst = create_engine(pg_url)

    target_meta = MetaData()
    target_meta.reflect(bind=dst)
    if not target_meta.tables:
        fail(
            "the target Postgres has no tables",
            "Bring the cron service up first: it is the only role that creates the shared schema.",
        )

    source_meta = MetaData()
    source_meta.reflect(bind=src)

    counts = {}
    with src.connect() as sconn, dst.begin() as dconn:
        for name in MASTER_TABLES:
            if name not in source_meta.tables:
                counts[name] = "absent in source"
                continue
            if name not in target_meta.tables:
                fail(f"{name} is missing from the target schema", "Is the cron service on the current release?")

            src_table = source_meta.tables[name]
            dst_table = target_meta.tables[name]

            if name not in MERGE_TABLES:
                existing = dconn.execute(select(dst_table)).first()
                if existing is not None:
                    fail(
                        f"{name} in Postgres already holds rows",
                        "This is meant for a fresh data tier. Empty it, or migrate into a clean database.",
                    )

            shared = [c.name for c in dst_table.columns if c.name in src_table.columns]
            rows = [
                {k: v for k, v in dict(r._mapping).items() if k in shared}
                for r in sconn.execute(select(*[src_table.c[c] for c in shared]))
            ]

            if name == "bot_text" and BOT_TEXT_MERGE_FILTER in shared:
                rows = [r for r in rows if r.get(BOT_TEXT_MERGE_FILTER)]
            if name == "system_setting":
                rows = [r for r in rows if r.get("key") not in SETTING_KEYS_NOT_CARRIED]

            if rows:
                if name in MERGE_TABLES:
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    keys = MERGE_TABLES[name]
                    stmt = pg_insert(dst_table).values(rows)
                    updatable = {c: stmt.excluded[c] for c in shared if c not in keys and c != "id"}
                    stmt = (
                        stmt.on_conflict_do_update(index_elements=keys, set_=updatable)
                        if updatable
                        else stmt.on_conflict_do_nothing(index_elements=keys)
                    )
                    dconn.execute(stmt)
                else:
                    dconn.execute(insert(dst_table), rows)
            counts[name] = len(rows)

        for table in target_meta.sorted_tables:
            for col in table.primary_key.columns:
                seq = dconn.exec_driver_sql(
                    "SELECT pg_get_serial_sequence(%s, %s)", (table.name, col.name)
                ).scalar()
                if not seq:
                    continue
                top = dconn.exec_driver_sql(f'SELECT max("{col.name}") FROM "{table.name}"').scalar()
                if top is not None:
                    dconn.exec_driver_sql("SELECT setval(%s, %s)", (seq, top))

    src.dispose()
    dst.dispose()
    return counts


def clean_node(sqlite_path):
    con = sqlite3.connect(sqlite_path)
    removed = {}
    try:
        present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in DROP_FROM_NODE:
            if name not in present:
                continue
            n = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            con.execute(f"DELETE FROM {name}")
            removed[name] = n

        if "system_setting" in present:
            keys = ",".join("?" for _ in NODE_SETTING_KEYS)
            n = con.execute(
                f"SELECT COUNT(*) FROM system_setting WHERE key NOT IN ({keys})", NODE_SETTING_KEYS
            ).fetchone()[0]
            con.execute(
                f"DELETE FROM system_setting WHERE key NOT IN ({keys})", NODE_SETTING_KEYS
            )
            removed["system_setting"] = n
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sqlite", required=True, help="the monolith's panel.db (a copy, not the live file)")
    parser.add_argument("--pg", help="shared Postgres URL; copies the master-side tables into it")
    parser.add_argument("--clean-node", action="store_true", help="delete master-side tables from --sqlite")
    parser.add_argument("--checkpoint-to", help="first take a WAL-consistent copy here and work on that")
    args = parser.parse_args(argv)

    if not args.pg and not args.clean_node:
        fail("nothing to do", "Pass --pg, --clean-node, or both.")

    source = args.sqlite
    if args.checkpoint_to:
        source = checkpoint(args.sqlite, args.checkpoint_to)
        print(f"  copied {args.sqlite} -> {source} (WAL included)")

    if args.pg:
        counts = copy_to_postgres(source, args.pg)
        print("\n  copied into Postgres:")
        for name in MASTER_TABLES:
            print(f"    {name:24} {counts.get(name, 0)}")

    if args.clean_node:
        removed = clean_node(source)
        print("\n  removed from the node database:")
        for name, n in removed.items():
            print(f"    {name:24} {n}")
        print("\n  kept on the node: " + ", ".join(NODE_ONLY_TABLES))

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
