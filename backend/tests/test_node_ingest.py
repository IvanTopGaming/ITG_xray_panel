def test_diff_snapshots_no_baseline_returns_empty():
    from app.jobs.panels import _diff_snapshots

    curr = {"inbounds": [{"tag": "vless", "up": 100, "down": 200, "clients": [{"email": "a", "up": 100, "down": 200}]}]}
    assert _diff_snapshots(None, curr) == []


def test_diff_snapshots_computes_positive_deltas():
    from app.jobs.panels import _diff_snapshots

    prev = {"inbounds": [{"tag": "vless", "up": 100, "down": 200, "clients": [{"email": "a", "up": 100, "down": 200}]}]}
    curr = {"inbounds": [{"tag": "vless", "up": 150, "down": 260, "clients": [{"email": "a", "up": 150, "down": 260}]}]}
    deltas = set(_diff_snapshots(prev, curr))
    assert ("user", "a", "vless", 50, 60) in deltas
    assert ("inbound", "vless", "", 50, 60) in deltas


def test_diff_snapshots_clamps_reset_to_zero():
    from app.jobs.panels import _diff_snapshots

    prev = {"inbounds": [{"tag": "vless", "up": 500, "down": 500, "clients": [{"email": "a", "up": 500, "down": 500}]}]}
    curr = {"inbounds": [{"tag": "vless", "up": 10, "down": 10, "clients": [{"email": "a", "up": 10, "down": 10}]}]}
    for d in _diff_snapshots(prev, curr):
        assert d[3] >= 0 and d[4] >= 0


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, val):
        self.store[key] = val.encode() if isinstance(val, str) else val


def _make_linked_panel(db, *, name="p1"):
    import time as _time

    from app.models import LinkedPanel

    panel = LinkedPanel(
        name=name,
        url="https://child.example.com",
        federation_token="tok",
        status="online",
        enable=True,
        created_at=int(_time.time()),
    )
    db.session.add(panel)
    db.session.commit()
    return panel


def test_poll_linked_panels_ingests_node_traffic_across_two_cycles(app, db):
    import time
    from unittest.mock import MagicMock, patch

    from app.jobs.panels import poll_linked_panels
    from app.models import NodeTrafficSnapshot

    panel = _make_linked_panel(db)

    cycle1 = {
        "timestamp": int(time.time()),
        "inbounds": [{"tag": "vless", "up": 1000, "down": 2000, "clients": [{"email": "a", "up": 1000, "down": 2000}]}],
    }
    cycle2 = {
        "timestamp": int(time.time()) + 600,
        "inbounds": [{"tag": "vless", "up": 1500, "down": 2600, "clients": [{"email": "a", "up": 1500, "down": 2600}]}],
    }

    fake_redis = _FakeRedis()
    client_mock = MagicMock()
    client_mock.snapshot.side_effect = [cycle1, cycle2]

    with (
        patch("app.jobs.panels.FederationClient", return_value=client_mock),
        patch("app.jobs.panels.get_redis", return_value=fake_redis),
    ):
        poll_linked_panels()
        assert NodeTrafficSnapshot.query.count() == 0

        poll_linked_panels()

    rows = NodeTrafficSnapshot.query.filter_by(panel_id=panel.id).all()
    user_row = next(r for r in rows if r.entity_type == "user" and r.entity_id == "a")
    inbound_row = next(r for r in rows if r.entity_type == "inbound" and r.entity_id == "vless")

    assert user_row.inbound_tag == "vless"
    assert user_row.up == 500
    assert user_row.down == 600
    assert inbound_row.up == 500
    assert inbound_row.down == 600
