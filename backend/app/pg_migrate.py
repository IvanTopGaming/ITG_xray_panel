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


_MIGRATION_LOCK_KEY = 84920001


def migrate_postgres_db(logger=None):
    is_pg = db.engine.dialect.name == "postgresql"
    lock_conn = None
    try:
        if is_pg:
            lock_conn = db.engine.connect()
            lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY})
            lock_conn.commit()
        db.create_all()
        dropped = _drop_foreign_keys()
        _ensure_schema_version()
        _seed_bot_texts_pg(force=False)
        reseeded = _maybe_force_reseed_bot_texts_pg()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        if lock_conn is not None:
            try:
                lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY})
                lock_conn.commit()
            except Exception:
                pass
            try:
                lock_conn.close()
            except Exception:
                pass
    if logger:
        logger.warning(
            "Postgres schema init: %s FK constraint(s) dropped, schema_version=%s, bot_texts_reseeded=%s",
            dropped,
            CURRENT_DB_VERSION,
            reseeded,
        )
    return {
        "new_version": CURRENT_DB_VERSION,
        "foreign_keys_dropped": dropped,
        "bot_texts_force_reseeded": reseeded,
    }


def _seed_bot_texts_pg(force=False):
    from db_migration import _load_bot_text_defaults

    defaults = _load_bot_text_defaults()
    if not defaults:
        return 0
    touched = 0
    for (key, lang), value in defaults.items():
        if force:
            res = db.session.execute(
                text(
                    "INSERT INTO bot_text (key, lang, text, customized) "
                    "VALUES (:key, :lang, :text, false) "
                    "ON CONFLICT (key, lang) DO UPDATE SET "
                    "text = excluded.text, updated_at = CURRENT_TIMESTAMP "
                    "WHERE bot_text.customized = false"
                ),
                {"key": key, "lang": lang, "text": value},
            )
        else:
            res = db.session.execute(
                text(
                    "INSERT INTO bot_text (key, lang, text, customized) "
                    "VALUES (:key, :lang, :text, false) "
                    "ON CONFLICT (key, lang) DO NOTHING"
                ),
                {"key": key, "lang": lang, "text": value},
            )
        touched += res.rowcount or 0
    return touched


def _maybe_force_reseed_bot_texts_pg():
    from db_migration import CURRENT_BOT_TEXTS_VERSION, _REMOVED_BOT_TEXT_KEYS

    row = db.session.execute(text("SELECT value FROM system_setting WHERE key = 'bot_texts_seeded_version'")).fetchone()
    try:
        stored = int(row[0]) if row else 1
    except (TypeError, ValueError):
        stored = 1
    if stored >= CURRENT_BOT_TEXTS_VERSION:
        return False
    if _REMOVED_BOT_TEXT_KEYS:
        db.session.execute(
            text("DELETE FROM bot_text WHERE key = ANY(:keys)"),
            {"keys": list(_REMOVED_BOT_TEXT_KEYS)},
        )
    _seed_bot_texts_pg(force=True)
    db.session.execute(
        text(
            "INSERT INTO system_setting (key, value) VALUES ('bot_texts_seeded_version', :v) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
        ),
        {"v": str(CURRENT_BOT_TEXTS_VERSION)},
    )
    return True
