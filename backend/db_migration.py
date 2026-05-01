import json
import os
import sqlite3
from typing import Dict, List, Optional, Tuple

CURRENT_DB_VERSION = 9


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
    """Create traffic_snapshot and domain_stat tables if they don't exist."""
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


def _ensure_node_table(cursor: sqlite3.Cursor) -> int:
    """Create the node table for multi-node management if it doesn't exist."""
    if _table_exists(cursor, "node"):
        changed = 0
        # NOTE: sync_inbound defaults to 0 here so existing nodes preserve the previous
        # "no inbound sync" behaviour. The SQLAlchemy model default is True, so any node
        # created via the API after migration will be opted in by default.
        for col, spec in [
            ("sync_users", "BOOLEAN NOT NULL DEFAULT 1"),
            ("sync_inbound", "BOOLEAN NOT NULL DEFAULT 0"),
            ("status", "VARCHAR(20) DEFAULT 'unknown'"),
            ("last_check", "BIGINT DEFAULT 0"),
            ("last_error", "TEXT DEFAULT ''"),
            ("groups", "TEXT NOT NULL DEFAULT ''"),
            ("strict_mirror", "BOOLEAN NOT NULL DEFAULT 0"),
        ]:
            if _add_column_if_missing(cursor, "node", col, spec):
                changed += 1
        return changed
    cursor.execute(
        """
        CREATE TABLE node (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL UNIQUE,
            url           TEXT    NOT NULL,
            username      TEXT    NOT NULL,
            password      TEXT    NOT NULL,
            inbound_tag   TEXT    NOT NULL,
            enable        BOOLEAN NOT NULL DEFAULT 1,
            sync_users    BOOLEAN NOT NULL DEFAULT 1,
            sync_inbound  BOOLEAN NOT NULL DEFAULT 1,
            status        VARCHAR(20) DEFAULT 'unknown',
            last_check    BIGINT DEFAULT 0,
            last_error    TEXT    DEFAULT '',
            groups        TEXT    NOT NULL DEFAULT '',
            strict_mirror BOOLEAN NOT NULL DEFAULT 0
        )
        """
    )
    return 1


def _ensure_node_client_traffic_table(cursor: sqlite3.Cursor) -> int:
    """Create the node_client_traffic table for per-node per-user aggregated traffic."""
    if _table_exists(cursor, "node_client_traffic"):
        return 0
    cursor.execute(
        """
        CREATE TABLE node_client_traffic (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id     INTEGER NOT NULL,
            email       TEXT    NOT NULL,
            up          BIGINT  DEFAULT 0,
            down        BIGINT  DEFAULT 0,
            last_polled BIGINT  DEFAULT 0,
            CONSTRAINT uq_nct UNIQUE (node_id, email)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_nct_email ON node_client_traffic (email)")
    return 1


def _ensure_client_device_table(cursor: sqlite3.Cursor) -> int:
    """Create the client_device table for device tracking (Stage 1)."""
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


def _ensure_schema_columns(cursor: sqlite3.Cursor) -> int:
    changed = 0

    schema_patches = [
        ("admin", "password_changed_at", "BIGINT NOT NULL DEFAULT 0"),
        ("routing_profile", "enable", "BOOLEAN NOT NULL DEFAULT 1"),
        ("outbound", "enable", "BOOLEAN NOT NULL DEFAULT 1"),
        ("outbound", "settings", "TEXT NOT NULL DEFAULT '{}'"),
        ("outbound", "stream_settings", "TEXT NOT NULL DEFAULT '{}'"),
        ("outbound", "mux", "TEXT NOT NULL DEFAULT '{}'"),
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
        ("client", "global_limit_bytes", "BIGINT DEFAULT 0"),
        ("client", "allowed_node_groups", "TEXT NOT NULL DEFAULT ''"),
        # Device tracking — Stage 1
        ("inbound", "device_limit", "INTEGER NOT NULL DEFAULT 0"),
        ("client", "device_limit", "INTEGER"),
        # Balancer fallback outbound
        ("balancer", "fallback_tag", "VARCHAR(50)"),
    ]

    for table_name, column_name, spec in schema_patches:
        if _add_column_if_missing(cursor, table_name, column_name, spec):
            changed += 1

    return changed


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
        node_table = _ensure_node_table(cursor)
        node_client_traffic_table = _ensure_node_client_traffic_table(cursor)
        client_device_table = _ensure_client_device_table(cursor)
        schema_changes = _ensure_schema_columns(cursor)
        removed_legacy_inbounds, normalized_streams = _cleanup_legacy_inbounds(cursor)
        fixed_rows = _apply_data_fixups(cursor)

        if old_version < CURRENT_DB_VERSION:
            _set_db_version(cursor, CURRENT_DB_VERSION)

        conn.commit()

        report = {
            "old_version": old_version,
            "new_version": CURRENT_DB_VERSION,
            "stats_tables_created": stats_tables,
            "node_table_created": node_table,
            "node_client_traffic_table_created": node_client_traffic_table,
            "client_device_table_created": client_device_table,
            "schema_changes": schema_changes,
            "removed_legacy_inbounds": removed_legacy_inbounds,
            "normalized_streams": normalized_streams,
            "fixed_rows": fixed_rows,
        }
        changed = (
            stats_tables > 0
            or node_table > 0
            or node_client_traffic_table > 0
            or client_device_table > 0
            or schema_changes > 0
            or removed_legacy_inbounds > 0
            or normalized_streams > 0
            or fixed_rows > 0
            or old_version < CURRENT_DB_VERSION
        )
        if logger and changed:
            logger.warning(
                "DB migration complete (v%s -> v%s): stats_tables=%s, node_table=%s, schema=%s, "
                "removed_legacy_inbounds=%s, normalized_streams=%s, fixed_rows=%s",
                old_version,
                CURRENT_DB_VERSION,
                stats_tables,
                node_table,
                schema_changes,
                removed_legacy_inbounds,
                normalized_streams,
                fixed_rows,
            )
        return report
    finally:
        conn.close()
