def test_app_fixture_provides_context(app):
    from flask import current_app

    assert current_app._get_current_object() is app


def test_db_fixture_can_create_table(app, db):
    from sqlalchemy import text

    db.session.execute(text("CREATE TABLE _smoke (id INTEGER PRIMARY KEY)"))
    db.session.execute(text("INSERT INTO _smoke (id) VALUES (1)"))
    db.session.commit()
    rows = db.session.execute(text("SELECT id FROM _smoke")).fetchall()
    assert len(rows) == 1


def test_sqlite_busy_timeout_is_bounded(app, db):
    from sqlalchemy import text

    assert db.session.execute(text("PRAGMA busy_timeout")).scalar() == 5000
