"""Splitting a 2.x monolith into the shared Postgres and one node's own SQLite.

The production master is a monolith: it runs its own Xray, holds 32 live clients of its own, and at
the same time owns every tariff, payment and Telegram user. The split deployment has no such role --
a master cannot have clients, and a node cannot reach Postgres -- so that one database has to become
two, and the machine keeps serving its keys as a node.

Three things decide whether that migration is safe, and each is a test here:

* **every table is accounted for.** A table nobody assigned goes nowhere, and nothing says so: the
  rows are simply absent afterwards. New tables arrive with almost every wave, so the guard is
  against the future rather than against today's schema.
* **secrets do not travel to the node.** `system_setting` holds `bot_token`, `yookassa_secret_key`
  and `bot_service_token` in clear text. A node is the least trusted machine in the deployment and
  needs three keys out of that table; handing it the rest would put the payment credentials on a box
  in somebody else's datacentre.
* **the node keeps what makes it a node.** Its inbounds, clients and its own federation token -- lose
  the last one and the master cannot see it at all.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from panel_core import split_monolith
from panel_core.extensions import db

DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pg_only = pytest.mark.skipif(not DSN, reason="DATABASE_URL_TEST not set")


def test_every_table_in_the_models_is_assigned_somewhere():
    """A table left out of all three lists is data that silently fails to arrive."""

    known = split_monolith.MASTER_TABLES | split_monolith.NODE_TABLES | split_monolith.DROPPED_TABLES
    modelled = {t.name for t in db.metadata.sorted_tables}

    unassigned = sorted(modelled - known)
    assert unassigned == [], (
        f"these tables exist in the models but the split does not mention them: {unassigned}. "
        f"Add each to MASTER_TABLES, NODE_TABLES or DROPPED_TABLES -- a table nobody assigns is "
        f"simply missing after the migration, with nothing reporting it."
    )

    stale = sorted(known - modelled - split_monolith.LEGACY_TABLES)
    assert stale == [], (
        f"the split names tables the models no longer have: {stale}. Either they were retired (move "
        f"them to LEGACY_TABLES, which is what the 2.x schema carries and 26 drops) or the name is a typo."
    )


def test_the_two_sides_do_not_overlap_except_where_intended():
    """Only the tables a node genuinely needs a copy of may appear on both sides."""

    both = split_monolith.MASTER_TABLES & split_monolith.NODE_TABLES
    assert both == split_monolith.SHARED_TABLES, (
        f"master and node both claim {sorted(both)}, but only {sorted(split_monolith.SHARED_TABLES)} "
        f"is meant to be duplicated. Anything else here means rows are written twice and then drift."
    )


def test_the_node_keeps_what_makes_it_a_node():
    for table in ("inbound", "client", "outbound", "routing_profile", "federation_config"):
        assert table in split_monolith.NODE_TABLES, (
            f"{table} does not reach the node. Without inbound/client it serves nobody; without "
            f"federation_config the master cannot see it at all."
        )


def test_payment_history_and_telegram_users_stay_off_the_node():
    for table in ("payment", "telegram_user", "user_tariff_access", "tariff", "linked_panel"):
        assert table not in split_monolith.NODE_TABLES, (
            f"{table} would be copied to the node. A node sits in somebody else's datacentre and its "
            f"federation token has been widened four times; payment history and Telegram identities "
            f"have no reason to be there, and nothing on that role reads them."
        )


def _monolith_db(path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE system_setting (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO system_setting (key, value) VALUES
            ('bot_token', '123:SECRET'),
            ('yookassa_secret_key', 'live_SECRET'),
            ('bot_service_token', 'svc-SECRET'),
            ('xray_log_level', 'warning'),
            ('geoip_url', 'https://example.com/geoip.dat'),
            ('geosite_url', 'https://example.com/geosite.dat');
        """
    )
    con.commit()
    con.close()


def test_only_the_xray_settings_follow_the_node(tmp_path):
    """system_setting is the one table that is split by row rather than by name.

    It carries the bot token, the YooKassa key and the bot service token beside the three keys a node
    genuinely reads -- the Xray log level and the two geo URLs. Copying the table wholesale is the
    easy mistake, and it puts every payment credential on the least trusted machine.
    """

    source = tmp_path / "panel.db"
    _monolith_db(source)
    target = tmp_path / "node.db"

    split_monolith.write_node_settings(str(source), str(target))

    con = sqlite3.connect(target)
    rows = dict(con.execute("SELECT key, value FROM system_setting").fetchall())
    con.close()

    assert set(rows) == set(split_monolith.NODE_SETTING_KEYS), (
        f"the node's system_setting carries {sorted(rows)}; it must carry exactly "
        f"{sorted(split_monolith.NODE_SETTING_KEYS)}."
    )
    for leaked in ("bot_token", "yookassa_secret_key", "bot_service_token"):
        assert leaked not in rows, f"{leaked} was copied to the node"


def test_it_refuses_a_database_that_has_not_been_migrated_yet(tmp_path):
    """v21 lacks columns the models already know about, so copying would die halfway through.

    The production monolith is at schema 21: linked_panel has `enable` where the models say
    `enabled`, and user_tariff_access has no `status` at all. A SELECT built from today's model
    definitions fails on the first such table -- after some rows have already been written.
    """

    source = tmp_path / "old.db"
    con = sqlite3.connect(source)
    con.execute("PRAGMA user_version = 21")
    con.execute("CREATE TABLE system_setting (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    con.close()

    with pytest.raises(split_monolith.MigrationRefused) as excinfo:
        split_monolith.require_migrated(str(source))

    assert "migrate_db.py" in str(excinfo.value), "the refusal must say how to fix it"


def test_a_live_database_is_copied_with_its_wal(tmp_path):
    """The production master had 11 MB of unmerged WAL beside its database.

    `cp panel.db` alone silently loses it -- and what sits in the WAL is the most recent writes, so
    the rows lost are the payments and grants somebody made minutes before the migration.
    """

    source = tmp_path / "live.db"
    con = sqlite3.connect(source)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("CREATE TABLE payment (id INTEGER PRIMARY KEY, amount INTEGER)")
    con.execute("INSERT INTO payment (amount) VALUES (100)")
    con.commit()
    con.execute("INSERT INTO payment (amount) VALUES (200)")
    con.commit()
    assert (tmp_path / "live.db-wal").exists(), "this test needs an unmerged WAL to be meaningful"

    copy = split_monolith.checkpoint_source(str(source), str(tmp_path / "copy.db"))
    con.close()

    check = sqlite3.connect(copy)
    total = check.execute("SELECT COUNT(*), SUM(amount) FROM payment").fetchone()
    check.close()
    assert total == (2, 300), f"the copy lost what was still in the WAL: {total}"


def _monolith_at_current_schema(path):
    """A miniature of the production master: its own inbound and clients, plus everything else."""

    from sqlalchemy import create_engine, insert

    engine = create_engine(f"sqlite:///{path}")
    db.metadata.create_all(engine)
    md = {t.name: t for t in db.metadata.sorted_tables}
    with engine.begin() as conn:
        conn.execute(insert(md["admin"]), [{"username": "admin", "password": "x"}])
        conn.execute(
            insert(md["system_setting"]),
            [
                {"key": "bot_token", "value": "123:SECRET"},
                {"key": "yookassa_secret_key", "value": "live_SECRET"},
                {"key": "xray_log_level", "value": "warning"},
            ],
        )
        conn.execute(
            insert(md["inbound"]),
            [{"tag": "okins", "protocol": "vless", "port": 443, "stream_settings": "{}"}],
        )
        conn.execute(
            insert(md["client"]),
            [{"id": "uuid-1", "inbound_tag": "okins", "email": "u1", "telegram_id": 4242}],
        )
        conn.execute(insert(md["telegram_user"]), [{"telegram_id": 4242, "language": "ru"}])
        conn.execute(insert(md["tariff"]), [{"name": "Base", "price_rub": 100, "period_days": 30}])
    engine.dispose()

    con = sqlite3.connect(path)
    con.execute(f"PRAGMA user_version = {split_monolith.REQUIRED_SCHEMA_VERSION}")
    con.commit()
    con.close()


@pg_only
def test_the_split_sends_each_table_to_exactly_one_side(tmp_path):
    from sqlalchemy import create_engine, text as sa_text

    source = tmp_path / "monolith.db"
    _monolith_at_current_schema(source)
    node_db = tmp_path / "node" / "panel.db"

    engine = create_engine(DSN)
    with engine.begin() as conn:
        conn.execute(sa_text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    engine.dispose()

    report = split_monolith.split(str(source), DSN, str(node_db))

    assert report["node"]["client"] == 1, "the node lost the clients it still has to serve"
    assert report["node"]["inbound"] == 1
    assert report["master"]["telegram_user"] == 1
    assert report["master"]["tariff"] == 1

    node = sqlite3.connect(node_db)
    node_settings = dict(node.execute("SELECT key, value FROM system_setting").fetchall())
    node_payments = node.execute("SELECT COUNT(*) FROM payment").fetchone()[0]
    node.close()
    assert node_settings == {"xray_log_level": "warning"}, (
        f"the node's settings are {node_settings}; the bot token and the YooKassa key must not be there"
    )
    assert node_payments == 0, "payment history reached the node"

    engine = create_engine(DSN)
    with engine.connect() as conn:
        pg_clients = conn.execute(sa_text("SELECT COUNT(*) FROM client")).scalar()
        pg_users = conn.execute(sa_text("SELECT COUNT(*) FROM telegram_user")).scalar()
    engine.dispose()
    assert pg_users == 1, "the master did not receive its Telegram users"
    assert pg_clients == 0, (
        "client rows reached the shared Postgres. A master cannot serve them -- it has no Xray -- and "
        "they would show up on its Dashboard as keys nobody can manage."
    )
