import json
import os
import sqlite3
import uuid
from typing import Dict, List, Optional, Tuple

CURRENT_DB_VERSION = 24
CURRENT_BOT_TEXTS_VERSION = 17


_REMOVED_BOT_TEXT_KEYS = (
    "checkout.button.cancel",
    "checkout.cancelled",
    "checkout.message",
    "checkout.success",
    "common.back",
    "errors.access_denied",
    "errors.payment_failed",
    "errors.tariff_not_available",
    "home.menu_header",
    "home.subscription_active",
    "home.title",
    "menu.keys",
    "menu.settings",
    "settings.language",
    "settings.language_changed",
    "sub.page.url_label",
    "tariff.button.buy",
    "tariff.button.renew",
    "tariff.unlimited_label",
)


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _table_columns(cursor: sqlite3.Cursor, table_name: str) -> List[str]:
    if not _table_exists(cursor, table_name):
        return []
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [str(row[1]) for row in cursor.fetchall()]


def _column_exists(cursor: sqlite3.Cursor, table_name: str, column_name: str) -> bool:
    return column_name in _table_columns(cursor, table_name)


def _index_exists(cursor: sqlite3.Cursor, index_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name = ? LIMIT 1",
        (index_name,),
    )
    return cursor.fetchone() is not None


def _add_column_if_missing(
    cursor: sqlite3.Cursor,
    table_name: str,
    column_name: str,
    sql_type_and_constraints: str,
) -> bool:
    if not _table_exists(cursor, table_name):
        return False
    if _column_exists(cursor, table_name, column_name):
        return False
    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {sql_type_and_constraints}")
    return True


def _normalize_legacy_stream_settings(raw_stream: Optional[str]) -> Tuple[Optional[str], bool]:
    try:
        stream = json.loads(raw_stream or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw_stream, False
    if not isinstance(stream, dict):
        return raw_stream, False

    changed = False

    reality_settings = stream.get("realitySettings", {})
    if isinstance(reality_settings, dict):
        if not str(reality_settings.get("publicKey", "") or "").strip():
            legacy_public = str(reality_settings.get("_publicKey", "") or "").strip()
            if legacy_public:
                reality_settings["publicKey"] = legacy_public
                changed = True
        if "_publicKey" in reality_settings:
            reality_settings.pop("_publicKey", None)
            changed = True
        stream["realitySettings"] = reality_settings

    tls_settings = stream.get("tlsSettings", {})
    if isinstance(tls_settings, dict):
        if not str(tls_settings.get("_utlsFingerprint", "") or "").strip():
            legacy_fp = str(
                tls_settings.get("utlsFingerprint", "") or tls_settings.get("fingerprint", "") or ""
            ).strip()
            if legacy_fp:
                tls_settings["_utlsFingerprint"] = legacy_fp
                changed = True
        if "utlsFingerprint" in tls_settings:
            tls_settings.pop("utlsFingerprint", None)
            changed = True
        if "fingerprint" in tls_settings:
            tls_settings.pop("fingerprint", None)
            changed = True
        stream["tlsSettings"] = tls_settings

    if "dokodemoNetwork" in stream:
        stream.pop("dokodemoNetwork", None)
        changed = True

    if not changed:
        return raw_stream, False
    return json.dumps(stream, ensure_ascii=False), True


def _ensure_stats_tables(cursor: sqlite3.Cursor) -> int:

    created = 0

    if not _table_exists(cursor, "traffic_snapshot"):
        cursor.execute(
            """
            CREATE TABLE traffic_snapshot (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT    NOT NULL,
                entity_id   TEXT    NOT NULL,
                inbound_tag TEXT    NOT NULL DEFAULT '',
                bucket      INTEGER NOT NULL,
                up          INTEGER DEFAULT 0,
                down        INTEGER DEFAULT 0,
                CONSTRAINT uq_ts UNIQUE (entity_type, entity_id, inbound_tag, bucket)
            )
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_ts_bucket ON traffic_snapshot (bucket)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_ts_entity ON traffic_snapshot (entity_type, entity_id, inbound_tag)"
        )
        created += 1

    if not _table_exists(cursor, "domain_stat"):
        cursor.execute(
            """
            CREATE TABLE domain_stat (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT    NOT NULL,
                domain       TEXT    NOT NULL,
                client_email TEXT    NOT NULL DEFAULT '',
                inbound_tag  TEXT    NOT NULL DEFAULT '',
                hit_count    INTEGER DEFAULT 0,
                CONSTRAINT uq_ds UNIQUE (date, domain, client_email, inbound_tag)
            )
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_ds_date ON domain_stat (date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_ds_domain ON domain_stat (domain)")
        created += 1

    return created


def _ensure_node_traffic_table(cursor: sqlite3.Cursor) -> int:
    if _table_exists(cursor, "node_traffic_snapshot"):
        return 0
    cursor.execute(
        """
        CREATE TABLE node_traffic_snapshot (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            panel_id    INTEGER NOT NULL,
            entity_type TEXT    NOT NULL,
            entity_id   TEXT    NOT NULL,
            inbound_tag TEXT    NOT NULL DEFAULT '',
            bucket      INTEGER NOT NULL,
            up          INTEGER DEFAULT 0,
            down        INTEGER DEFAULT 0,
            CONSTRAINT uq_nts UNIQUE (panel_id, entity_type, entity_id, inbound_tag, bucket)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_nts_panel_bucket ON node_traffic_snapshot (panel_id, bucket)")
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_nts_bucket ON node_traffic_snapshot (bucket)")
    return 1


def _ensure_notification_claim_table(cursor: sqlite3.Cursor) -> int:
    if _table_exists(cursor, "notification_claim"):
        return 0
    cursor.execute(
        """
        CREATE TABLE notification_claim (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id BIGINT  NOT NULL,
            tariff_id   INTEGER NOT NULL DEFAULT 0,
            scope       TEXT    NOT NULL DEFAULT '',
            kind        TEXT    NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_notification_claim UNIQUE (telegram_id, tariff_id, scope, kind)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_notification_claim_tariff ON notification_claim (telegram_id, tariff_id)"
    )
    return 1


def _ensure_provision_receipt_table(cursor: sqlite3.Cursor) -> int:
    if _table_exists(cursor, "provision_receipt"):
        return 0
    cursor.execute(
        """
        CREATE TABLE provision_receipt (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT   NOT NULL,
            inbound_tag     TEXT   NOT NULL,
            telegram_id     BIGINT NOT NULL,
            response_json   TEXT   NOT NULL,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_provision_receipt UNIQUE (idempotency_key, inbound_tag)
        )
        """
    )
    return 1


def _ensure_stats_indexes(cursor: sqlite3.Cursor) -> int:
    created = 0
    composite_indexes = [
        (
            "traffic_snapshot",
            "ix_ts_type_bucket",
            "CREATE INDEX IF NOT EXISTS ix_ts_type_bucket ON traffic_snapshot (entity_type, bucket)",
        ),
        (
            "domain_stat",
            "ix_ds_date_domain",
            "CREATE INDEX IF NOT EXISTS ix_ds_date_domain ON domain_stat (date, domain)",
        ),
    ]
    for table_name, index_name, sql in composite_indexes:
        if not _table_exists(cursor, table_name):
            continue
        already = _index_exists(cursor, index_name)
        cursor.execute(sql)
        if not already:
            created += 1
    return created


def _ensure_stats_cover_indexes(cursor: sqlite3.Cursor) -> int:
    created = 0
    cover_indexes = [
        (
            "traffic_snapshot",
            "ix_ts_type_bucket_cover",
            "CREATE INDEX IF NOT EXISTS ix_ts_type_bucket_cover "
            "ON traffic_snapshot (entity_type, bucket, entity_id, inbound_tag, up, down)",
        ),
        (
            "domain_stat",
            "ix_ds_date_domain_cover",
            "CREATE INDEX IF NOT EXISTS ix_ds_date_domain_cover "
            "ON domain_stat (date, domain, client_email, inbound_tag, hit_count)",
        ),
    ]
    for table_name, index_name, sql in cover_indexes:
        if not _table_exists(cursor, table_name):
            continue
        already = _index_exists(cursor, index_name)
        cursor.execute(sql)
        if not already:
            created += 1
    return created


def _ensure_linked_panel_table(cursor: sqlite3.Cursor) -> int:
    if _table_exists(cursor, "linked_panel"):
        return 0
    cursor.execute("""
        CREATE TABLE linked_panel (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL UNIQUE,
            url              TEXT    NOT NULL,
            federation_token TEXT    NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'unknown',
            last_poll        BIGINT,
            last_error       TEXT,
            enable           BOOLEAN NOT NULL DEFAULT 1,
            created_at       BIGINT  NOT NULL
        )
    """)
    return 1


def _ensure_federation_config_table(cursor: sqlite3.Cursor) -> int:
    if _table_exists(cursor, "federation_config"):
        return 0
    cursor.execute("""
        CREATE TABLE federation_config (
            id                INTEGER PRIMARY KEY CHECK (id = 1),
            master_url        TEXT,
            master_name       TEXT,
            federation_token  TEXT,
            link_token        TEXT,
            link_token_used   BOOLEAN NOT NULL DEFAULT 0,
            linked_at         BIGINT
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO federation_config (id) VALUES (1)")
    return 1


def _ensure_client_device_table(cursor: sqlite3.Cursor) -> int:

    if _table_exists(cursor, "client_device"):
        return 0
    cursor.execute(
        """
        CREATE TABLE client_device (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   VARCHAR(128) NOT NULL,
            hwid        VARCHAR(128) NOT NULL,
            device_os   VARCHAR(32)  DEFAULT '',
            os_ver      VARCHAR(32)  DEFAULT '',
            model       VARCHAR(128) DEFAULT '',
            user_agent  VARCHAR(512) DEFAULT '',
            request_ip  VARCHAR(64)  DEFAULT '',
            first_seen  BIGINT       NOT NULL,
            last_seen   BIGINT       NOT NULL,
            hits        INTEGER      DEFAULT 1,
            FOREIGN KEY (client_id) REFERENCES client(id) ON DELETE CASCADE
        )
        """
    )
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_client_hwid ON client_device(client_id, hwid)")
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_client_device_client_id ON client_device(client_id)")
    return 1


def _ensure_billing_tables(cursor: sqlite3.Cursor) -> int:

    statements = [
        (
            "tariff",
            """
            CREATE TABLE IF NOT EXISTS tariff (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(120) NOT NULL,
                price_rub INTEGER NOT NULL,
                period_days INTEGER NOT NULL,
                visibility VARCHAR(16) NOT NULL DEFAULT 'public',
                is_trial BOOLEAN NOT NULL DEFAULT 0,
                enabled BOOLEAN NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
        (
            "tariff_item",
            """
            CREATE TABLE IF NOT EXISTS tariff_item (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tariff_id INTEGER NOT NULL REFERENCES tariff(id) ON DELETE CASCADE,
                inbound_tag VARCHAR(120) NOT NULL,
                label VARCHAR(60),
                traffic_gb INTEGER NOT NULL,
                panel_id INTEGER REFERENCES linked_panel(id),
                sort_order INTEGER NOT NULL DEFAULT 0
            )
            """,
        ),
        (
            "user_tariff_access",
            """
            CREATE TABLE IF NOT EXISTS user_tariff_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id BIGINT NOT NULL,
                tariff_id INTEGER NOT NULL REFERENCES tariff(id) ON DELETE CASCADE,
                billing VARCHAR(8) NOT NULL,
                next_renewal_at DATETIME,
                note VARCHAR(255),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_user_tariff UNIQUE (telegram_id, tariff_id)
            )
            """,
        ),
        (
            "payment",
            """
            CREATE TABLE IF NOT EXISTS payment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                yookassa_id VARCHAR(64) NOT NULL UNIQUE,
                telegram_id BIGINT NOT NULL,
                tariff_id INTEGER NOT NULL REFERENCES tariff(id),
                tariff_snapshot TEXT NOT NULL,
                amount_rub INTEGER NOT NULL,
                status VARCHAR(16) NOT NULL,
                confirmation_url TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                paid_at DATETIME
            )
            """,
        ),
        (
            "bot_text",
            """
            CREATE TABLE IF NOT EXISTS bot_text (
                key VARCHAR(120) NOT NULL,
                lang VARCHAR(8) NOT NULL,
                text TEXT NOT NULL,
                customized INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (key, lang)
            )
            """,
        ),
        (
            "bot_event",
            """
            CREATE TABLE IF NOT EXISTS bot_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type VARCHAR(32) NOT NULL,
                telegram_id BIGINT,
                payload TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                delivered_at DATETIME
            )
            """,
        ),
        (
            "telegram_user",
            """
            CREATE TABLE IF NOT EXISTS telegram_user (
                telegram_id BIGINT PRIMARY KEY,
                username VARCHAR(64),
                language VARCHAR(8) NOT NULL DEFAULT 'ru',
                trial_used_at DATETIME,
                blocked BOOLEAN NOT NULL DEFAULT 0,
                first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                note VARCHAR(255)
            )
            """,
        ),
        (
            "notification_log",
            """
            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id BIGINT NOT NULL,
                client_id VARCHAR(128) NOT NULL REFERENCES client(id) ON DELETE CASCADE,
                kind VARCHAR(32) NOT NULL,
                sent_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
    ]

    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_tariff_visibility ON tariff(visibility)",
        "CREATE INDEX IF NOT EXISTS ix_tariff_item_tariff ON tariff_item(tariff_id)",
        "CREATE INDEX IF NOT EXISTS ix_uta_telegram ON user_tariff_access(telegram_id)",
        "CREATE INDEX IF NOT EXISTS ix_uta_renewal ON user_tariff_access(next_renewal_at)",
        "CREATE INDEX IF NOT EXISTS ix_payment_telegram ON payment(telegram_id)",
        "CREATE INDEX IF NOT EXISTS ix_payment_created ON payment(created_at)",
        "CREATE INDEX IF NOT EXISTS ix_bot_event_telegram ON bot_event(telegram_id)",
        "CREATE INDEX IF NOT EXISTS ix_bot_event_created ON bot_event(created_at)",
        "CREATE INDEX IF NOT EXISTS ix_notif_dedup ON notification_log(telegram_id, client_id, kind, sent_at)",
    ]

    created = 0
    for table_name, sql in statements:
        if not _table_exists(cursor, table_name):
            cursor.execute(sql)
            created += 1
    for idx_sql in indexes:
        cursor.execute(idx_sql)
    return created


def _alter_client_billing_columns(cursor: sqlite3.Cursor) -> int:

    if not _table_exists(cursor, "client"):
        return 0
    added = 0
    if _add_column_if_missing(cursor, "client", "telegram_id", "BIGINT"):
        added += 1
    if _add_column_if_missing(cursor, "client", "tariff_id", "INTEGER"):
        added += 1
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_client_telegram ON client(telegram_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_client_tariff ON client(tariff_id)")
    return added


def _read_bot_text_defaults_source(defaults_path: Optional[str] = None) -> Optional[str]:

    if defaults_path is None:
        from panel_core.resources import BOT_TEXTS_DEFAULTS, read_data_text

        return read_data_text(BOT_TEXTS_DEFAULTS)
    if not os.path.exists(defaults_path):
        return None
    with open(defaults_path, "r", encoding="utf-8") as fh:
        return fh.read()


def _seed_bot_texts(
    cursor: sqlite3.Cursor,
    defaults_path: Optional[str] = None,
    *,
    force: bool = False,
) -> int:

    try:
        import yaml
    except ImportError:
        return 0

    raw = _read_bot_text_defaults_source(defaults_path)
    if raw is None:
        return 0

    data = yaml.safe_load(raw)

    if not data or not isinstance(data, dict):
        return 0

    touched = 0
    for key, langs in data.items():
        if not isinstance(langs, dict):
            continue
        for lang, text in langs.items():
            if force:
                cursor.execute(
                    """
                    INSERT INTO bot_text (key, lang, text, customized)
                    VALUES (?, ?, ?, 0)
                    ON CONFLICT(key, lang) DO UPDATE SET
                        text = excluded.text,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE customized = 0
                    """,
                    (key, lang, str(text)),
                )
                touched += 1
            else:
                cursor.execute(
                    "SELECT 1 FROM bot_text WHERE key=? AND lang=?",
                    (key, lang),
                )
                if cursor.fetchone() is not None:
                    continue
                cursor.execute(
                    "INSERT INTO bot_text (key, lang, text, customized) VALUES (?, ?, ?, 0)",
                    (key, lang, str(text)),
                )
                touched += 1
    return touched


def _load_bot_text_defaults(defaults_path: Optional[str] = None) -> Dict[Tuple[str, str], str]:

    try:
        import yaml
    except ImportError:
        return {}
    raw = _read_bot_text_defaults_source(defaults_path)
    if raw is None:
        return {}
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        return {}
    out: Dict[Tuple[str, str], str] = {}
    for key, langs in data.items():
        if not isinstance(langs, dict):
            continue
        for lang, text in langs.items():
            out[(key, lang)] = str(text)
    return out


def _ensure_bot_text_customized_column(cursor: sqlite3.Cursor) -> int:

    if not _add_column_if_missing(cursor, "bot_text", "customized", "INTEGER NOT NULL DEFAULT 0"):
        return 0
    defaults = _load_bot_text_defaults()
    if not defaults:
        return 0
    marked = 0
    cursor.execute("SELECT key, lang, text FROM bot_text")
    for key, lang, text in cursor.fetchall():
        default = defaults.get((key, lang))
        if default is not None and text != default:
            cursor.execute("UPDATE bot_text SET customized=1 WHERE key=? AND lang=?", (key, lang))
            marked += 1
    return marked


def _maybe_force_reseed_bot_texts(cursor: sqlite3.Cursor) -> bool:

    if not _table_exists(cursor, "system_setting"):
        return False
    cursor.execute("SELECT value FROM system_setting WHERE key='bot_texts_seeded_version'")
    row = cursor.fetchone()
    try:
        stored = int(row[0]) if row else 1
    except (TypeError, ValueError):
        stored = 1

    if stored >= CURRENT_BOT_TEXTS_VERSION:
        return False

    if _REMOVED_BOT_TEXT_KEYS:
        placeholders = ",".join("?" * len(_REMOVED_BOT_TEXT_KEYS))
        cursor.execute(
            f"DELETE FROM bot_text WHERE key IN ({placeholders})",
            _REMOVED_BOT_TEXT_KEYS,
        )

    _seed_bot_texts(cursor, force=True)

    cursor.execute(
        """
        INSERT INTO system_setting (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        ("bot_texts_seeded_version", str(CURRENT_BOT_TEXTS_VERSION)),
    )
    return True


def _ensure_schema_columns(cursor: sqlite3.Cursor) -> int:
    changed = 0

    schema_patches = [
        ("admin", "password_changed_at", "BIGINT NOT NULL DEFAULT 0"),
        ("routing_profile", "enable", "BOOLEAN NOT NULL DEFAULT 1"),
        ("outbound", "enable", "BOOLEAN NOT NULL DEFAULT 1"),
        ("outbound", "settings", "TEXT NOT NULL DEFAULT '{}'"),
        ("outbound", "stream_settings", "TEXT NOT NULL DEFAULT '{}'"),
        ("outbound", "mux", "TEXT NOT NULL DEFAULT '{}'"),
        ("outbound", "send_through", "VARCHAR(50)"),
        ("outbound", "public_ip", "VARCHAR(50)"),
        ("outbound", "gateway", "VARCHAR(50)"),
        ("balancer", "enable", "BOOLEAN NOT NULL DEFAULT 1"),
        ("balancer", "selector", "TEXT NOT NULL DEFAULT '[]'"),
        ("balancer", "strategy", "VARCHAR(20) DEFAULT 'random'"),
        ("inbound", "routing_profile_id", "INTEGER"),
        ("inbound", "up", "BIGINT DEFAULT 0"),
        ("inbound", "down", "BIGINT DEFAULT 0"),
        ("inbound", "fallback_address", "VARCHAR(100)"),
        ("client", "limit_bytes", "BIGINT DEFAULT 0"),
        ("client", "expiry_time", "BIGINT DEFAULT 0"),
        ("client", "up", "BIGINT DEFAULT 0"),
        ("client", "down", "BIGINT DEFAULT 0"),
        ("client", "enable", "BOOLEAN DEFAULT 1"),
        ("client", "reset_day", "INTEGER DEFAULT 0"),
        ("client", "last_reset_time", "BIGINT DEFAULT 0"),
        ("client", "last_seen", "BIGINT DEFAULT 0"),
        ("client", "source_ips", "TEXT DEFAULT '[]'"),
        ("client", "flow", "VARCHAR(50)"),
        ("client", "preferred_outbound", "VARCHAR(50)"),
        ("inbound", "device_limit", "INTEGER NOT NULL DEFAULT 0"),
        ("client", "device_limit", "INTEGER"),
        ("balancer", "fallback_tag", "VARCHAR(50)"),
        ("telegram_user", "language_chosen", "BOOLEAN NOT NULL DEFAULT 0"),
        ("inbound", "label", "VARCHAR(60)"),
        ("payment", "chat_id", "BIGINT"),
        ("payment", "message_id", "INTEGER"),
        ("tariff_item", "panel_id", "INTEGER REFERENCES linked_panel(id)"),
        ("telegram_user", "sub_token", "VARCHAR(36)"),
    ]

    for table_name, column_name, spec in schema_patches:
        if _add_column_if_missing(cursor, table_name, column_name, spec):
            changed += 1

    return changed


def _backfill_sub_tokens(cursor: sqlite3.Cursor) -> int:

    if not _table_exists(cursor, "telegram_user"):
        return 0
    if not _column_exists(cursor, "telegram_user", "sub_token"):
        return 0

    cursor.execute("SELECT telegram_id FROM telegram_user WHERE sub_token IS NULL OR sub_token = ''")
    rows = [r[0] for r in cursor.fetchall()]
    for tg_id in rows:
        cursor.execute(
            "UPDATE telegram_user SET sub_token = ? WHERE telegram_id = ?",
            (str(uuid.uuid4()), tg_id),
        )

    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_telegram_user_sub_token ON telegram_user(sub_token)")
    return len(rows)


def _cleanup_legacy_inbounds(cursor: sqlite3.Cursor) -> Tuple[int, int]:
    if not _table_exists(cursor, "inbound"):
        return 0, 0

    normalized_streams = 0
    legacy_tags: List[str] = []
    legacy_ids: List[int] = []

    cursor.execute("SELECT id, tag, protocol, stream_settings FROM inbound")
    rows = cursor.fetchall()
    for row in rows:
        inbound_id = row[0]
        tag = str(row[1] or "").strip()
        protocol = str(row[2] or "").strip().lower()
        raw_stream = row[3]

        if protocol == "dokodemo-door":
            legacy_ids.append(int(inbound_id))
            if tag:
                legacy_tags.append(tag)
            continue

        normalized_stream, changed = _normalize_legacy_stream_settings(raw_stream)
        if changed:
            cursor.execute(
                "UPDATE inbound SET stream_settings = ? WHERE id = ?",
                (normalized_stream, inbound_id),
            )
            normalized_streams += 1

    if legacy_tags and _table_exists(cursor, "client"):
        for tag in legacy_tags:
            cursor.execute("DELETE FROM client WHERE inbound_tag = ?", (tag,))

    for inbound_id in legacy_ids:
        cursor.execute("DELETE FROM inbound WHERE id = ?", (inbound_id,))

    removed_legacy_inbounds = len(legacy_ids)
    return removed_legacy_inbounds, normalized_streams


def _apply_data_fixups(cursor: sqlite3.Cursor) -> int:
    changed = 0

    def _run_if_column(table_name: str, column_name: str, query: str) -> None:
        nonlocal changed
        if _column_exists(cursor, table_name, column_name):
            cursor.execute(query)
            changed += int(cursor.rowcount or 0)

    _run_if_column(
        "admin",
        "password_changed_at",
        "UPDATE admin SET password_changed_at = 0 WHERE password_changed_at IS NULL",
    )
    _run_if_column(
        "routing_profile",
        "enable",
        "UPDATE routing_profile SET enable = 1 WHERE enable IS NULL",
    )
    _run_if_column(
        "outbound",
        "enable",
        "UPDATE outbound SET enable = 1 WHERE enable IS NULL",
    )
    _run_if_column(
        "balancer",
        "enable",
        "UPDATE balancer SET enable = 1 WHERE enable IS NULL",
    )
    _run_if_column(
        "client",
        "enable",
        "UPDATE client SET enable = 1 WHERE enable IS NULL",
    )
    _run_if_column(
        "client",
        "source_ips",
        "UPDATE client SET source_ips = '[]' WHERE source_ips IS NULL OR TRIM(source_ips) = ''",
    )
    _run_if_column(
        "client",
        "flow",
        "UPDATE client SET flow = '' WHERE flow IS NULL",
    )
    _run_if_column(
        "client",
        "preferred_outbound",
        "UPDATE client SET preferred_outbound = NULL WHERE preferred_outbound IS NOT NULL AND TRIM(preferred_outbound) = ''",
    )

    _run_if_column(
        "linked_panel",
        "created_at",
        "UPDATE linked_panel SET created_at = created_at * 1000 WHERE created_at > 0 AND created_at < 100000000000",
    )

    _run_if_column(
        "linked_panel",
        "last_poll",
        "UPDATE linked_panel SET last_poll = last_poll * 1000 "
        "WHERE last_poll IS NOT NULL AND last_poll > 0 AND last_poll < 100000000000",
    )

    return changed


def _get_db_version(cursor: sqlite3.Cursor) -> int:
    cursor.execute("PRAGMA user_version")
    row = cursor.fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _set_db_version(cursor: sqlite3.Cursor, version: int) -> None:
    cursor.execute(f"PRAGMA user_version = {int(version)}")


def migrate_sqlite_db(db_path: str, logger=None) -> Dict[str, int]:
    if not db_path:
        raise ValueError("db_path is required")

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        old_version = _get_db_version(cursor)

        stats_tables = _ensure_stats_tables(cursor)
        node_traffic_table = _ensure_node_traffic_table(cursor)
        notification_claim_table = _ensure_notification_claim_table(cursor)
        provision_receipt_table = _ensure_provision_receipt_table(cursor)
        stats_indexes = _ensure_stats_indexes(cursor)
        stats_cover_indexes = _ensure_stats_cover_indexes(cursor)
        linked_panel_table = _ensure_linked_panel_table(cursor)
        federation_config_table = _ensure_federation_config_table(cursor)
        client_device_table = _ensure_client_device_table(cursor)
        billing_tables = _ensure_billing_tables(cursor)
        client_billing_columns = _alter_client_billing_columns(cursor)
        bot_texts_seeded = _seed_bot_texts(cursor)
        bot_texts_customized_marked = _ensure_bot_text_customized_column(cursor)
        bot_texts_force_reseeded = _maybe_force_reseed_bot_texts(cursor)
        schema_changes = _ensure_schema_columns(cursor)
        sub_tokens_backfilled = _backfill_sub_tokens(cursor)
        removed_legacy_inbounds, normalized_streams = _cleanup_legacy_inbounds(cursor)
        fixed_rows = _apply_data_fixups(cursor)

        if old_version < CURRENT_DB_VERSION:
            _set_db_version(cursor, CURRENT_DB_VERSION)

        conn.commit()

        report = {
            "old_version": old_version,
            "new_version": CURRENT_DB_VERSION,
            "stats_tables_created": stats_tables,
            "node_traffic_table_created": node_traffic_table,
            "notification_claim_table_created": notification_claim_table,
            "provision_receipt_table_created": provision_receipt_table,
            "stats_indexes_created": stats_indexes,
            "stats_cover_indexes_created": stats_cover_indexes,
            "linked_panel_table_created": linked_panel_table,
            "federation_config_table_created": federation_config_table,
            "client_device_table_created": client_device_table,
            "billing_tables_created": billing_tables,
            "client_billing_columns_added": client_billing_columns,
            "bot_texts_seeded": bot_texts_seeded,
            "bot_texts_customized_marked": bot_texts_customized_marked,
            "bot_texts_force_reseeded": bot_texts_force_reseeded,
            "schema_changes": schema_changes,
            "sub_tokens_backfilled": sub_tokens_backfilled,
            "removed_legacy_inbounds": removed_legacy_inbounds,
            "normalized_streams": normalized_streams,
            "fixed_rows": fixed_rows,
        }
        changed = (
            stats_tables > 0
            or node_traffic_table > 0
            or notification_claim_table > 0
            or provision_receipt_table > 0
            or stats_indexes > 0
            or stats_cover_indexes > 0
            or linked_panel_table > 0
            or federation_config_table > 0
            or client_device_table > 0
            or billing_tables > 0
            or client_billing_columns > 0
            or bot_texts_seeded > 0
            or bot_texts_customized_marked > 0
            or bot_texts_force_reseeded
            or schema_changes > 0
            or sub_tokens_backfilled > 0
            or removed_legacy_inbounds > 0
            or normalized_streams > 0
            or fixed_rows > 0
            or old_version < CURRENT_DB_VERSION
        )
        if logger and changed:
            logger.warning(
                "DB migration complete (v%s -> v%s): stats_tables=%s, linked_panel=%s, "
                "federation_config=%s, schema=%s, "
                "removed_legacy_inbounds=%s, normalized_streams=%s, fixed_rows=%s",
                old_version,
                CURRENT_DB_VERSION,
                stats_tables,
                linked_panel_table,
                federation_config_table,
                schema_changes,
                removed_legacy_inbounds,
                normalized_streams,
                fixed_rows,
            )
        return report
    finally:
        conn.close()
