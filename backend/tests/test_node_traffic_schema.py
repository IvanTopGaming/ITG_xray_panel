def test_current_db_version_is_24():
    from panel_core.db_migration import CURRENT_DB_VERSION

    assert CURRENT_DB_VERSION == 24


def test_node_traffic_table_created_on_sqlite(tmp_path):
    import sqlite3

    from panel_core.db_migration import migrate_sqlite_db

    db_path = str(tmp_path / "panel.db")
    migrate_sqlite_db(db_path)
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(node_traffic_snapshot)").fetchall()}
    conn.close()
    assert {"panel_id", "entity_type", "entity_id", "inbound_tag", "bucket", "up", "down"}.issubset(cols)


def test_node_traffic_model_registered(app):
    from panel_core.extensions import db

    assert "node_traffic_snapshot" in db.metadata.tables
