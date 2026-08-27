from sqlalchemy import inspect, text

from panel_core.extensions import db
from panel_core.db_migration import CURRENT_DB_VERSION, RETIRED_TABLES


def _drop_foreign_keys():
    if db.engine.dialect.name != "postgresql":
        return 0
    rows = db.session.execute(
        text("SELECT conrelid::regclass AS tbl, conname FROM pg_constraint WHERE contype = 'f'")
    ).fetchall()
    for tbl, conname in rows:
        db.session.execute(text('ALTER TABLE {} DROP CONSTRAINT IF EXISTS "{}"'.format(tbl, conname)))
    return len(rows)


def _drop_retired_tables():

    dropped = 0
    inspector = inspect(db.engine)
    existing = set(inspector.get_table_names())
    for table in RETIRED_TABLES:
        if table not in existing:
            continue
        db.session.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
        dropped += 1
    return dropped


PG_DEAD_TABLES = ("traffic_snapshot", "domain_stat", "notification_log")


def _create_all_except_dead_tables(skip_dead=False):
    if not skip_dead or db.engine.dialect.name != "postgresql":
        db.create_all()
        return
    tables = [table for name, table in db.metadata.tables.items() if name not in PG_DEAD_TABLES]
    db.metadata.create_all(bind=db.engine, tables=tables)


def _drop_dead_tables(logger=None):
    if db.engine.dialect.name != "postgresql":
        return [], []
    inspector = inspect(db.engine)
    existing = set(inspector.get_table_names())
    dropped, kept = [], []
    for table in PG_DEAD_TABLES:
        if table not in existing:
            continue
        rows = db.session.execute(text(f'SELECT count(*) FROM "{table}"')).scalar() or 0
        if rows:
            kept.append(table)
            if logger:
                logger.warning(
                    "Postgres schema: %s holds %s row(s) from an earlier monolithic install and was left alone — "
                    "nothing on this role reads it, but dropping it would destroy that history",
                    table,
                    rows,
                )
            continue
        db.session.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
        dropped.append(table)
    return dropped, kept


def _column_ddl(column, dialect=None) -> tuple[str, bool]:
    dialect = dialect or db.engine.dialect
    type_sql = column.type.compile(dialect=dialect)
    pieces = [f'"{column.name}" {type_sql}']
    server_default = getattr(column, "server_default", None)
    default_sql = ""
    if server_default is not None and getattr(server_default, "arg", None) is not None:
        arg = server_default.arg
        default_sql = str(arg.text if hasattr(arg, "text") else arg)
        pieces.append(f"DEFAULT {default_sql}")
    forced_nullable = False
    if not column.nullable:
        if default_sql:
            pieces.append("NOT NULL")
        else:
            forced_nullable = True
    return " ".join(pieces), forced_nullable


def _add_missing_columns(logger=None) -> int:
    if db.engine.dialect.name != "postgresql":
        return 0
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    added = 0
    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl, forced_nullable = _column_ddl(column)
            db.session.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}'))
            added += 1
            if logger:
                if forced_nullable:
                    logger.warning(
                        "Postgres schema: added %s.%s as NULLABLE — the model declares NOT NULL but gives no "
                        "server_default, and an existing table cannot be filled from here",
                        table.name,
                        column.name,
                    )
                else:
                    logger.warning("Postgres schema: added missing column %s.%s", table.name, column.name)
    return added


def _ensure_schema_version():
    db.session.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version integer not null)"))
    existing = db.session.execute(text("SELECT version FROM schema_version LIMIT 1")).fetchone()
    if existing is None:
        db.session.execute(text("INSERT INTO schema_version (version) VALUES (:v)"), {"v": CURRENT_DB_VERSION})
    else:
        db.session.execute(text("UPDATE schema_version SET version = :v"), {"v": CURRENT_DB_VERSION})


_MIGRATION_LOCK_KEY = 84920001


def migrate_postgres_db(logger=None, *, drop_dead_tables=False):
    is_pg = db.engine.dialect.name == "postgresql"
    lock_conn = None
    columns_added = 0
    dead_dropped: list = []
    dead_kept: list = []
    try:
        if is_pg:
            lock_conn = db.engine.connect()
            lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY})
            lock_conn.commit()
        _create_all_except_dead_tables(skip_dead=drop_dead_tables)
        columns_added = _add_missing_columns(logger=logger)
        if drop_dead_tables:
            dead_dropped, dead_kept = _drop_dead_tables(logger=logger)
        retired = _drop_retired_tables()
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
            "Postgres schema init: %s FK constraint(s) dropped, %s retired table(s) dropped, "
            "%s column(s) added, dead tables dropped=%s kept=%s, schema_version=%s, bot_texts_reseeded=%s",
            dropped,
            retired,
            columns_added,
            dead_dropped,
            dead_kept,
            CURRENT_DB_VERSION,
            reseeded,
        )
    return {
        "new_version": CURRENT_DB_VERSION,
        "foreign_keys_dropped": dropped,
        "retired_tables_dropped": retired,
        "columns_added": columns_added,
        "dead_tables_dropped": dead_dropped,
        "dead_tables_kept": dead_kept,
        "bot_texts_force_reseeded": reseeded,
    }


def _seed_bot_texts_pg(force=False):
    from panel_core.db_migration import _load_bot_text_defaults

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
    from panel_core.db_migration import CURRENT_BOT_TEXTS_VERSION, _REMOVED_BOT_TEXT_KEYS

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
