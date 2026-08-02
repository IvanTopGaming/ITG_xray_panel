"""The shared Postgres stops carrying three tables nothing on it reads (wave 9, §96).

`traffic_snapshot`, `domain_stat` and `notification_log` are written by `services/stats.py` and
`services/notifications.py`, both of which run under jobs only `roles/worker.py` registers, against
a node's own SQLite. On the roles that sit on the shared Postgres nothing reaches them: statistics
answers 501 before touching the database (wave 4d), the local provisioning branch is refused by
`_require_local_xray` before it clears a `NotificationLog`, and `cleanup_stats_job` is a worker job.

The external review listed ten such tables. Seven of them were wrong: `client`, `inbound`,
`outbound`, `balancer` and `routing_profile` have live readers on the master; `provision_receipt` is
pruned by the cron service; `federation_config` is read by `_check_federation_token`, so dropping it
would turn a 401 into a 500.
"""

import os

import pytest

DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pg_only = pytest.mark.skipif(not DSN, reason="DATABASE_URL_TEST not set")


def test_the_list_names_only_tables_nothing_on_postgres_touches():
    from panel_core.pg_migrate import PG_DEAD_TABLES

    assert set(PG_DEAD_TABLES) == {"traffic_snapshot", "domain_stat", "notification_log"}


def test_sqlite_keeps_them(app, db):
    from sqlalchemy import inspect

    from panel_core.extensions import db as _db

    names = set(inspect(_db.engine).get_table_names())
    assert {"traffic_snapshot", "domain_stat", "notification_log"} <= names


def test_only_the_cron_role_asks_for_the_drop():
    """The flag lives at the call site, like `bootstrap_defaults(system_outbounds=False)`.

    A node's compose omits DATABASE_URL rather than forbidding it, so a worker pointed at Postgres
    would take the same migration path — and these three tables are the only ones it genuinely
    writes. Dropping them from inside the shared function would delete a node's own statistics.
    """

    import inspect as py_inspect

    from panel_core.roles import cron, worker

    assert "drop_dead_tables=True" in py_inspect.getsource(cron.create_app)
    assert "drop_dead_tables" not in py_inspect.getsource(worker.create_app)


def test_the_drop_is_off_by_default(app, db):
    import inspect as py_inspect

    from panel_core import pg_migrate

    signature = py_inspect.signature(pg_migrate.migrate_postgres_db)
    assert signature.parameters["drop_dead_tables"].default is False


def _pg_app():
    from flask import Flask

    import panel_core.models  # noqa: F401
    from panel_core.extensions import db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = DSN
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


@pg_only
def test_empty_dead_tables_are_dropped_and_stay_dropped():
    from sqlalchemy import inspect, text

    from panel_core.extensions import db
    from panel_core.pg_migrate import migrate_postgres_db

    app = _pg_app()
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()

        migrate_postgres_db(drop_dead_tables=True)
        names = set(inspect(db.engine).get_table_names())
        assert "traffic_snapshot" not in names
        assert "domain_stat" not in names
        assert "notification_log" not in names

        migrate_postgres_db(drop_dead_tables=True)
        names = set(inspect(db.engine).get_table_names())
        assert "traffic_snapshot" not in names


@pg_only
def test_a_dead_table_with_rows_survives_and_is_reported():
    from sqlalchemy import inspect, text

    from panel_core.extensions import db
    from panel_core.pg_migrate import migrate_postgres_db

    app = _pg_app()
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()

        db.create_all()
        db.session.execute(
            text(
                "INSERT INTO domain_stat (date, domain, client_email, inbound_tag, hit_count) "
                "VALUES ('2026-01-01', 'example.com', 'a@b', 'tag', 3)"
            )
        )
        db.session.commit()

        report = migrate_postgres_db(drop_dead_tables=True)

        names = set(inspect(db.engine).get_table_names())
        assert "domain_stat" in names
        assert "domain_stat" in report["dead_tables_kept"]
        assert "traffic_snapshot" in report["dead_tables_dropped"]
        assert db.session.execute(text("SELECT count(*) FROM domain_stat")).scalar() == 1


@pg_only
def test_the_live_tables_the_review_wanted_dropped_are_still_there():
    from sqlalchemy import inspect, text

    from panel_core.extensions import db
    from panel_core.pg_migrate import migrate_postgres_db

    app = _pg_app()
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()

        migrate_postgres_db(drop_dead_tables=True)

        names = set(inspect(db.engine).get_table_names())
        for table in (
            "client",
            "inbound",
            "outbound",
            "balancer",
            "routing_profile",
            "provision_receipt",
            "federation_config",
        ):
            assert table in names, table


@pg_only
def test_without_the_flag_a_postgres_backed_node_keeps_its_statistics():
    from sqlalchemy import inspect, text

    from panel_core.extensions import db
    from panel_core.pg_migrate import migrate_postgres_db

    app = _pg_app()
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()

        report = migrate_postgres_db()

        names = set(inspect(db.engine).get_table_names())
        assert {"traffic_snapshot", "domain_stat", "notification_log"} <= names
        assert report["dead_tables_dropped"] == []
