import os

import pytest

DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pg_only = pytest.mark.skipif(not DSN, reason="DATABASE_URL_TEST not set")


def _pg_ctx():
    from flask import Flask

    from panel_core.extensions import db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = DSN
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app, db


@pg_only
def test_snapshot_upsert_accumulates_on_conflict():
    from sqlalchemy import text

    import panel_core.models  # noqa: F401

    app, db = _pg_ctx()
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()
        from panel_core.pg_migrate import migrate_postgres_db

        migrate_postgres_db()

        from panel_core.services.stats import _upsert_snapshot

        _upsert_snapshot("user", 1, "tag", 1000, 10, 20)
        _upsert_snapshot("user", 1, "tag", 1000, 5, 7)
        db.session.commit()

        up, down = db.session.execute(
            text("SELECT up, down FROM traffic_snapshot WHERE entity_type='user' AND entity_id='1'")
        ).fetchone()
        assert up == 15
        assert down == 27


@pg_only
def test_domain_stat_upsert_accumulates_on_conflict():
    from sqlalchemy import text

    import panel_core.models  # noqa: F401

    app, db = _pg_ctx()
    with app.app_context():
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()
        from panel_core.pg_migrate import migrate_postgres_db

        migrate_postgres_db()

        from panel_core.services.stats import _upsert_domain_stat

        _upsert_domain_stat("2026-07-24", "example.com", "u@x", "tag", 3)
        _upsert_domain_stat("2026-07-24", "example.com", "u@x", "tag", 4)
        db.session.commit()

        hits = db.session.execute(
            text("SELECT hit_count FROM domain_stat WHERE domain='example.com' AND client_email='u@x'")
        ).scalar()
        assert hits == 7
