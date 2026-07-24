from sqlalchemy import text

from app.extensions import db
from db_migration import CURRENT_DB_VERSION


def _drop_foreign_keys():
    if db.engine.dialect.name != "postgresql":
        return 0
    rows = db.session.execute(
        text("SELECT conrelid::regclass AS tbl, conname FROM pg_constraint WHERE contype = 'f'")
    ).fetchall()
    for tbl, conname in rows:
        db.session.execute(text('ALTER TABLE {} DROP CONSTRAINT IF EXISTS "{}"'.format(tbl, conname)))
    return len(rows)


def _ensure_schema_version():
    db.session.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version integer not null)"))
    existing = db.session.execute(text("SELECT version FROM schema_version LIMIT 1")).fetchone()
    if existing is None:
        db.session.execute(text("INSERT INTO schema_version (version) VALUES (:v)"), {"v": CURRENT_DB_VERSION})
    else:
        db.session.execute(text("UPDATE schema_version SET version = :v"), {"v": CURRENT_DB_VERSION})


def migrate_postgres_db(logger=None):
    db.create_all()
    dropped = _drop_foreign_keys()
    _ensure_schema_version()
    db.session.commit()
    if logger:
        logger.warning(
            "Postgres schema init: %s FK constraint(s) dropped, schema_version=%s", dropped, CURRENT_DB_VERSION
        )
    return {
        "new_version": CURRENT_DB_VERSION,
        "foreign_keys_dropped": dropped,
        "bot_texts_force_reseeded": False,
    }
