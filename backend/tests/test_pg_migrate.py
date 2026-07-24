import os

import pytest


def test_drop_foreign_keys_noop_on_sqlite(app):
    from app.pg_migrate import _drop_foreign_keys

    assert _drop_foreign_keys() == 0


def test_ensure_schema_version_sets_current(app):
    from sqlalchemy import text

    from app.extensions import db
    from app.pg_migrate import _ensure_schema_version
    from db_migration import CURRENT_DB_VERSION

    _ensure_schema_version()
    db.session.commit()
    row = db.session.execute(text("SELECT version FROM schema_version")).fetchone()
    assert row[0] == CURRENT_DB_VERSION


def test_ensure_schema_version_is_idempotent(app):
    from sqlalchemy import text

    from app.extensions import db
    from app.pg_migrate import _ensure_schema_version
    from db_migration import CURRENT_DB_VERSION

    _ensure_schema_version()
    _ensure_schema_version()
    db.session.commit()
    rows = db.session.execute(text("SELECT version FROM schema_version")).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == CURRENT_DB_VERSION


DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pg_only = pytest.mark.skipif(not DSN, reason="DATABASE_URL_TEST not set")


@pg_only
def test_migrate_postgres_db_leaves_no_enforced_fks():
    from sqlalchemy import text

    from app.extensions import db
    import app.models  # noqa: F401
    from app.pg_migrate import migrate_postgres_db

    from flask import Flask

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = DSN
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()
        report = migrate_postgres_db()
        fk_count = db.session.execute(text("SELECT count(*) FROM pg_constraint WHERE contype='f'")).scalar()
        assert fk_count == 0
        assert report["new_version"] > 0
        assert report["foreign_keys_dropped"] >= 0
        assert report["bot_texts_force_reseeded"] is False
