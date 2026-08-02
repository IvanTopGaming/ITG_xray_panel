import os

import pytest

DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pg_only = pytest.mark.skipif(not DSN, reason="DATABASE_URL_TEST not set")


def test_add_missing_columns_is_a_noop_off_postgres(app):
    from panel_core.pg_migrate import _add_missing_columns

    assert _add_missing_columns() == 0


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
def test_add_missing_columns_adds_a_column_the_model_grew():
    from sqlalchemy import text

    from panel_core.extensions import db
    from panel_core.pg_migrate import _add_missing_columns, migrate_postgres_db

    app = _pg_app()
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()
        migrate_postgres_db()

        db.session.execute(text("ALTER TABLE provision_receipt DROP COLUMN materialized"))
        db.session.execute(
            text(
                "INSERT INTO provision_receipt (idempotency_key, inbound_tag, telegram_id, response_json) "
                "VALUES ('k1', 'tag', 1, '{}')"
            )
        )
        db.session.commit()

        added = _add_missing_columns()
        db.session.commit()

        cols = {
            r[0]
            for r in db.session.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'provision_receipt'")
            ).fetchall()
        }
        assert "materialized" in cols
        assert added >= 1

        row = db.session.execute(
            text("SELECT materialized FROM provision_receipt WHERE idempotency_key = 'k1'")
        ).fetchone()
        assert row[0] is False


@pg_only
def test_add_missing_columns_never_drops_a_column_the_model_lost():
    from sqlalchemy import text

    from panel_core.extensions import db
    from panel_core.pg_migrate import _add_missing_columns, migrate_postgres_db

    app = _pg_app()
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()
        migrate_postgres_db()

        db.session.execute(text("ALTER TABLE provision_receipt ADD COLUMN legacy_extra TEXT"))
        db.session.commit()

        _add_missing_columns()
        db.session.commit()

        cols = {
            r[0]
            for r in db.session.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'provision_receipt'")
            ).fetchall()
        }
        assert "legacy_extra" in cols


@pg_only
def test_not_null_without_server_default_is_added_nullable_instead_of_failing():
    from sqlalchemy import Column, String, text

    from panel_core.extensions import db
    from panel_core.pg_migrate import _add_missing_columns, migrate_postgres_db

    app = _pg_app()
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()
        migrate_postgres_db()
        db.session.execute(
            text(
                "INSERT INTO provision_receipt (idempotency_key, inbound_tag, telegram_id, response_json) "
                "VALUES ('k2', 'tag', 1, '{}')"
            )
        )
        db.session.commit()

        table = db.metadata.tables["provision_receipt"]
        probe = Column("probe_no_default", String(16), nullable=False)
        table.append_column(probe)
        try:
            added = _add_missing_columns()
            db.session.commit()
            assert added >= 1
            row = db.session.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'provision_receipt' AND column_name = 'probe_no_default'"
                )
            ).fetchone()
            assert row[0] == "YES"
        finally:
            table._columns.remove(probe)
