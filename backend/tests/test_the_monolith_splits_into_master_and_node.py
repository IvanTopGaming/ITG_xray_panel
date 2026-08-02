"""Moving a 2.x monolith's data into the split deployment.

The production master is a monolith: its own Xray, 32 live clients, and at the same time every
tariff, payment and Telegram user. The split architecture has no role like that, so the machine
keeps its keys and becomes a node while the rest moves to the new master's Postgres.

`scripts/migrate_2x.py` does that, and it is deliberately standalone -- one file, `pip install
sqlalchemy psycopg2-binary`, no checkout of this repo and no `panel_core` import. That is the
constraint it is written to: on the day of the migration you fetch one file onto the data tier and
run it. Which is also why the table lists live inside it and this file checks them against the
models -- a copy of a list is a list that goes stale.

Three properties matter:

* **every table is accounted for** -- one nobody assigned is data that silently fails to arrive;
* **secrets do not stay on the node** -- `system_setting` holds `bot_token`, `yookassa_secret_key`
  and `bot_service_token` in clear text, and a node is the least trusted machine in the deployment;
* **the node keeps what makes it a node** -- inbounds, clients, and its own federation token.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import sys

import pytest

from panel_core.extensions import db

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import migrate_2x  # noqa: E402

DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pg_only = pytest.mark.skipif(not DSN, reason="DATABASE_URL_TEST not set")

LEGACY_TABLES = {"client_device", "node_traffic_snapshot"}


def test_every_table_in_the_models_is_accounted_for():
    known = set(migrate_2x.MASTER_TABLES) | set(migrate_2x.NODE_ONLY_TABLES)
    modelled = {t.name for t in db.metadata.sorted_tables}

    unassigned = sorted(modelled - known)
    assert unassigned == [], (
        f"these tables exist in the models but scripts/migrate_2x.py mentions neither side: "
        f"{unassigned}. Add each to MASTER_TABLES or NODE_ONLY_TABLES -- a table nobody assigns is "
        f"simply absent after the migration, with nothing reporting it."
    )

    stale = sorted(known - modelled - LEGACY_TABLES)
    assert stale == [], f"the script names tables the models no longer have: {stale}"


def test_the_two_sides_do_not_overlap():
    both = set(migrate_2x.MASTER_TABLES) & set(migrate_2x.NODE_ONLY_TABLES)
    assert both == set(), f"{sorted(both)} would be copied twice and then drift apart"


def test_the_node_keeps_what_makes_it_a_node():
    for table in ("inbound", "client", "outbound", "routing_profile", "federation_config"):
        assert table in migrate_2x.NODE_ONLY_TABLES, (
            f"{table} is not kept on the node. Without inbound/client it serves nobody; without "
            f"federation_config the master cannot see it at all."
        )
    for table in ("inbound", "client", "federation_config", "provision_receipt"):
        assert table not in migrate_2x.DROP_FROM_NODE, f"{table} would be deleted from the node"


def test_payment_history_is_removed_from_the_node():
    for table in ("payment", "telegram_user", "user_tariff_access", "tariff", "linked_panel"):
        assert table in migrate_2x.DROP_FROM_NODE, (
            f"{table} would stay on the node. A node sits in somebody else's datacentre and its "
            f"federation token has been widened four times; payment history and Telegram identities "
            f"have no reason to be there, and nothing on that role reads them."
        )


def _monolith(path, *, version=migrate_2x.REQUIRED_SCHEMA):
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
    con.execute(f"PRAGMA user_version = {version}")
    con.commit()
    con.close()


def test_cleaning_the_node_keeps_its_keys_and_drops_the_secrets(tmp_path):
    node_db = tmp_path / "node.db"
    _monolith(node_db)

    migrate_2x.clean_node(str(node_db))

    con = sqlite3.connect(node_db)
    settings = dict(con.execute("SELECT key, value FROM system_setting").fetchall())
    clients = con.execute("SELECT COUNT(*) FROM client").fetchone()[0]
    inbounds = con.execute("SELECT COUNT(*) FROM inbound").fetchone()[0]
    users = con.execute("SELECT COUNT(*) FROM telegram_user").fetchone()[0]
    con.close()

    assert clients == 1 and inbounds == 1, "the node lost the keys it still has to serve"
    assert users == 0, "Telegram users stayed on the node"
    assert settings == {"xray_log_level": "warning"}, (
        f"the node kept {sorted(settings)}; only the Xray-facing keys may remain, and bot_token "
        f"and yookassa_secret_key must not."
    )


def test_it_refuses_a_database_that_has_not_been_migrated_yet(tmp_path):
    """v21 columns disagree with the models, so a copy would die partway through.

    The production monolith is at 21: linked_panel has `enable` where the models say `enabled`, and
    user_tariff_access has no `status`. The node migrates its own schema on start-up, so the fix is
    to boot it once -- and the refusal says exactly that.
    """

    source = tmp_path / "old.db"
    _monolith(source, version=21)

    with pytest.raises(SystemExit):
        migrate_2x.copy_to_postgres(str(source), "postgresql+psycopg2://unused/unused")


def test_a_live_database_is_copied_with_its_wal(tmp_path):
    """The production master had 11 MB of unmerged WAL beside its 84 MB database.

    `cp panel.db` alone loses it, and what sits in the WAL is the most recent writes -- the payments
    and grants made minutes before the migration.
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

    copy = migrate_2x.checkpoint(str(source), str(tmp_path / "copy.db"))
    con.close()

    check = sqlite3.connect(copy)
    total = check.execute("SELECT COUNT(*), SUM(amount) FROM payment").fetchone()
    check.close()
    assert total == (2, 300), f"the copy lost what was still in the WAL: {total}"


@pytest.fixture
def scratch_dsn():
    """A database of this test's own.

    The suite shares DATABASE_URL_TEST with every other Postgres test. The first version of this
    test emptied it with DROP SCHEMA, which passed locally against a private database and then took
    out test_sub_mode in CI: the master refuses to boot on a schema that does not exist, which is
    exactly what it had just been handed.
    """

    from sqlalchemy import create_engine, text as sa_text

    name = f"migrate2x_scratch_{os.getpid()}"
    admin = create_engine(DSN.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa_text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(sa_text(f'CREATE DATABASE "{name}"'))
    yield DSN.rsplit("/", 1)[0] + "/" + name
    with admin.connect() as conn:
        conn.execute(sa_text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pg_only
def test_the_master_side_reaches_postgres_and_the_clients_do_not(tmp_path, scratch_dsn):
    from sqlalchemy import create_engine, text as sa_text

    source = tmp_path / "monolith.db"
    _monolith(source)

    engine = create_engine(scratch_dsn)
    db.metadata.create_all(engine)
    engine.dispose()

    counts = migrate_2x.copy_to_postgres(str(source), scratch_dsn)

    assert counts["telegram_user"] == 1
    assert counts["tariff"] == 1

    engine = create_engine(scratch_dsn)
    with engine.connect() as conn:
        pg_clients = conn.execute(sa_text("SELECT COUNT(*) FROM client")).scalar()
        pg_users = conn.execute(sa_text("SELECT COUNT(*) FROM telegram_user")).scalar()
    engine.dispose()

    assert pg_users == 1, "the master did not receive its Telegram users"
    assert pg_clients == 0, (
        "client rows reached the shared Postgres. A master cannot serve them -- it has no Xray -- "
        "and they would show up on its Dashboard as keys nobody can manage."
    )


@pg_only
def test_it_refuses_a_postgres_that_already_holds_rows(tmp_path, scratch_dsn):
    """Running it twice, or against a live deployment, must not double the payment history."""

    from sqlalchemy import create_engine

    source = tmp_path / "monolith.db"
    _monolith(source)

    engine = create_engine(scratch_dsn)
    db.metadata.create_all(engine)
    engine.dispose()

    migrate_2x.copy_to_postgres(str(source), scratch_dsn)
    with pytest.raises(SystemExit):
        migrate_2x.copy_to_postgres(str(source), scratch_dsn)
