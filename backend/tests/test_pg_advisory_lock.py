import os

import pytest

DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pg_only = pytest.mark.skipif(not DSN, reason="DATABASE_URL_TEST not set")


@pg_only
def test_migrate_releases_advisory_lock():
    from flask import Flask
    from sqlalchemy import text

    from app.extensions import db
    import app.models  # noqa: F401
    from app.pg_migrate import migrate_postgres_db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = DSN
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()
        migrate_postgres_db()
        held = db.session.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory'")).scalar()
        assert held == 0


@pg_only
def test_migrate_exception_path_releases_lock_after_rollback(monkeypatch):
    from flask import Flask
    from sqlalchemy import text

    from app.extensions import db
    import app.models  # noqa: F401
    import app.pg_migrate as pg_migrate_module

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = DSN
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()

        def _boom():
            db.session.execute(text("SELECT * FROM nonexistent_table_xyz"))

        monkeypatch.setattr(pg_migrate_module, "_ensure_schema_version", _boom)

        with pytest.raises(Exception):
            pg_migrate_module.migrate_postgres_db()

        held = db.session.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory'")).scalar()
        assert held == 0
