import os

import pytest

DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pg_only = pytest.mark.skipif(not DSN, reason="DATABASE_URL_TEST not set")


def _pg_app():
    from flask import Flask

    from app.extensions import db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = DSN
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app, db


def _reset_and_create(db):
    from sqlalchemy import text

    import app.models  # noqa: F401

    db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    db.session.commit()
    db.create_all()


@pg_only
def test_seed_inserts_defaults_and_is_idempotent():
    from sqlalchemy import text

    app, db = _pg_app()
    with app.app_context():
        _reset_and_create(db)
        from app.pg_migrate import _seed_bot_texts_pg

        first = _seed_bot_texts_pg(force=False)
        db.session.commit()
        count1 = db.session.execute(text("SELECT count(*) FROM bot_text")).scalar()
        second = _seed_bot_texts_pg(force=False)
        db.session.commit()
        count2 = db.session.execute(text("SELECT count(*) FROM bot_text")).scalar()

        assert first > 0
        assert count1 == count2
        assert second == 0


@pg_only
def test_force_seed_preserves_customized_rows():
    from sqlalchemy import text

    app, db = _pg_app()
    with app.app_context():
        _reset_and_create(db)
        from app.pg_migrate import _seed_bot_texts_pg

        _seed_bot_texts_pg(force=False)
        db.session.commit()
        row = db.session.execute(text("SELECT key, lang FROM bot_text LIMIT 1")).fetchone()
        key, lang = row[0], row[1]
        db.session.execute(
            text("UPDATE bot_text SET text='EDITED', customized=true WHERE key=:k AND lang=:l"),
            {"k": key, "l": lang},
        )
        db.session.commit()

        _seed_bot_texts_pg(force=True)
        db.session.commit()
        kept = db.session.execute(
            text("SELECT text FROM bot_text WHERE key=:k AND lang=:l"), {"k": key, "l": lang}
        ).scalar()
        assert kept == "EDITED"


@pg_only
def test_maybe_force_reseed_bumps_version_and_purges_removed_keys():
    from sqlalchemy import text

    from db_migration import CURRENT_BOT_TEXTS_VERSION, _REMOVED_BOT_TEXT_KEYS

    app, db = _pg_app()
    with app.app_context():
        _reset_and_create(db)
        from app.pg_migrate import _maybe_force_reseed_bot_texts_pg

        if _REMOVED_BOT_TEXT_KEYS:
            db.session.execute(
                text("INSERT INTO bot_text (key, lang, text, customized) VALUES (:k, 'ru', 'x', false)"),
                {"k": _REMOVED_BOT_TEXT_KEYS[0]},
            )
            db.session.commit()

        did = _maybe_force_reseed_bot_texts_pg()
        db.session.commit()
        assert did is True

        stored = db.session.execute(
            text("SELECT value FROM system_setting WHERE key='bot_texts_seeded_version'")
        ).scalar()
        assert int(stored) == CURRENT_BOT_TEXTS_VERSION

        if _REMOVED_BOT_TEXT_KEYS:
            leftover = db.session.execute(
                text("SELECT count(*) FROM bot_text WHERE key=:k"), {"k": _REMOVED_BOT_TEXT_KEYS[0]}
            ).scalar()
            assert leftover == 0

        assert _maybe_force_reseed_bot_texts_pg() is False


@pg_only
def test_migrate_postgres_db_seeds_bot_texts():
    from sqlalchemy import text

    app, db = _pg_app()
    with app.app_context():
        from sqlalchemy import text as _t

        import app.models  # noqa: F401

        db.session.execute(_t("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()

        from app.pg_migrate import migrate_postgres_db

        report = migrate_postgres_db()
        n = db.session.execute(text("SELECT count(*) FROM bot_text")).scalar()
        langs = db.session.execute(text("SELECT count(DISTINCT lang) FROM bot_text")).scalar()

        assert n > 0
        assert langs == 2
        assert report["bot_texts_force_reseeded"] is True
