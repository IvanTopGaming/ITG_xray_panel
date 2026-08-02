from flask import Flask

import panel_core.models  # noqa: F401  -- registers tables with db.metadata
from panel_core.app_base import run_startup_migration
from panel_core.extensions import db

_SQLITE_PREFIX = "sqlite:///"


def ensure_schema(database_url):

    app = Flask("schema-fixture")
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    sqlite_path = database_url[len(_SQLITE_PREFIX) :] if database_url.startswith(_SQLITE_PREFIX) else ""
    with app.app_context():
        run_startup_migration(app, sqlite_path)
        db.session.remove()
        db.engine.dispose()
    return database_url
