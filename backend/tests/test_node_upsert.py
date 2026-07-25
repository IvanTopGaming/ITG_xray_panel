def test_upsert_node_snapshot_accumulates_sqlite(app):
    from sqlalchemy import text

    from panel_core.extensions import db
    from panel_core.services.stats import _upsert_node_snapshot

    _upsert_node_snapshot(3, "user", "tg1_vless", "vless", 1000, 10, 20)
    _upsert_node_snapshot(3, "user", "tg1_vless", "vless", 1000, 5, 7)
    db.session.commit()
    up, down = db.session.execute(
        text("SELECT up, down FROM node_traffic_snapshot WHERE panel_id=3 AND entity_id='tg1_vless'")
    ).fetchone()
    assert up == 15
    assert down == 27


def test_upsert_node_snapshot_zero_is_noop(app):
    from sqlalchemy import text

    from panel_core.extensions import db
    from panel_core.services.stats import _upsert_node_snapshot

    _upsert_node_snapshot(3, "user", "x", "vless", 1000, 0, 0)
    db.session.commit()
    n = db.session.execute(text("SELECT count(*) FROM node_traffic_snapshot")).scalar()
    assert n == 0
