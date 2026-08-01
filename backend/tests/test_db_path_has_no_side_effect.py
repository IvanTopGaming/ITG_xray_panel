"""`db_path()` names a file; it does not create anything (wave 10).

It used to `os.makedirs("./db")` on the way out, so the three roles that run on the shared Postgres
each left an empty `db/` in their container — a directory that reads as "the database belongs here"
while the role's data is somewhere else entirely. That is the §48 trap in miniature: a script logged
`db_path()` while the ORM used `DATABASE_URL`, and the wrong file was read for half an hour.

The directory is now created where it is used: the SQLite branch of `run_startup_migration`, before
`db.create_all()` touches the file, and `migrate_sqlite_db` makes it too.
"""

import os

from panel_core.app_base import db_path


def test_db_path_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    path = db_path()

    assert path == os.path.join(str(tmp_path), "db", "panel.db")
    assert not os.path.exists(os.path.join(str(tmp_path), "db"))


def test_db_path_is_stable_across_calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert db_path() == db_path()
    assert not os.path.exists(os.path.join(str(tmp_path), "db"))


def test_the_sqlite_migration_creates_the_folder_it_needs(tmp_path):
    from panel_core.db_migration import migrate_sqlite_db

    target = tmp_path / "db" / "panel.db"
    assert not target.parent.exists()

    migrate_sqlite_db(str(target))

    assert target.exists()


def test_startup_migration_on_sqlite_creates_the_folder_before_create_all(tmp_path, monkeypatch):
    from flask import Flask

    import panel_core.models  # noqa: F401
    from panel_core.app_base import run_startup_migration
    from panel_core.extensions import db

    monkeypatch.chdir(tmp_path)
    target = os.path.join(str(tmp_path), "db", "panel.db")
    assert not os.path.exists(os.path.dirname(target))

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{target}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        run_startup_migration(app, target, seed_bot_texts=False)

    assert os.path.exists(target)
