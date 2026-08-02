from unittest.mock import patch


def _make_app(uri):
    from flask import Flask

    from panel_core.extensions import db  # noqa: F401

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    return app


def test_sqlite_uri_uses_sqlite_migration():
    from panel_core.app_base import run_startup_migration

    app = _make_app("sqlite:///:memory:")
    with (
        patch("panel_core.app_base.migrate_sqlite_db", return_value={}) as m_sqlite,
        patch("panel_core.app_base.migrate_postgres_db", return_value={}) as m_pg,
        patch("panel_core.app_base.db.create_all") as m_create,
    ):
        with app.app_context():
            run_startup_migration(app, "/tmp/panel.db")
    m_sqlite.assert_called_once()
    m_pg.assert_not_called()
    m_create.assert_called_once()


def test_postgres_uri_uses_postgres_migration():
    from panel_core.app_base import run_startup_migration

    app = _make_app("postgresql+psycopg2://u:p@h/db")
    with (
        patch("panel_core.app_base.migrate_sqlite_db", return_value={}) as m_sqlite,
        patch("panel_core.app_base.migrate_postgres_db", return_value={"bot_texts_force_reseeded": False}) as m_pg,
    ):
        with app.app_context():
            run_startup_migration(app, "/tmp/panel.db")
    m_pg.assert_called_once()
    m_sqlite.assert_not_called()
