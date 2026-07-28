"""§8.5: the node-traffic ingest pipeline is gone.

`poll_linked_panels` used to diff consecutive snapshots and write the deltas into
`node_traffic_snapshot`. That table had writers and **no readers anywhere** — the master's
Statistics endpoints read `traffic_snapshot` and `domain_stat`, and nothing writes either of
those into the master's Postgres. Statistics were declared node-local, so the transport was
deleted instead of being given a consumer.

The table itself still exists; dropping it is a schema change and belongs to the migration
wave. These tests pin the absence of the pipeline, which is the part that would creep back.
"""

import ast

from tests.import_graph import source_path


def test_diff_snapshots_helper_is_gone():
    import panel_core.jobs.panels as panels_job

    assert not hasattr(panels_job, "_diff_snapshots")


def test_node_snapshot_upsert_is_gone():
    from panel_core.services import stats, traffic_store

    assert not hasattr(traffic_store, "_upsert_node_snapshot")
    assert not hasattr(stats, "_upsert_node_snapshot"), "the worker-side re-export must go too"


def test_poll_linked_panels_owns_only_health_fields():
    source = source_path("jobs/panels.py").read_text()
    tree = ast.parse(source)

    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "_upsert_node_snapshot" not in called
    assert "_ten_min_bucket" not in called

    assigned = {
        target.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
    }
    assert assigned <= {"status", "last_poll", "last_error"}, (
        f"poll_linked_panels should only own LinkedPanel health fields, got {sorted(assigned)}"
    )


def test_poll_linked_panels_still_refreshes_status_and_snapshot_cache(app, db):
    """The health-poller half must survive the ingest removal."""
    import time
    from unittest.mock import MagicMock, patch

    from panel_core.jobs.panels import poll_linked_panels
    from panel_core.models import LinkedPanel

    panel = LinkedPanel(
        name="p1",
        url="https://child.example.com",
        federation_token="tok",
        status="unknown",
        enable=True,
        created_at=int(time.time() * 1000),
    )
    db.session.add(panel)
    db.session.commit()

    snapshot = {
        "timestamp": int(time.time() * 1000),
        "inbounds": [{"tag": "vless", "up": 1000, "down": 2000, "clients": [{"email": "a"}]}],
    }

    written = {}

    class _FakeRedis:
        def setex(self, key, ttl, val):
            written[key] = val

    client_mock = MagicMock()
    client_mock.snapshot.return_value = snapshot

    with (
        patch("panel_core.jobs.panels.FederationClient", return_value=client_mock),
        patch("panel_core.services.panel_proxy.get_shared_redis", return_value=_FakeRedis()),
    ):
        poll_linked_panels()

    assert db.session.get(LinkedPanel, panel.id).status == "online"
    assert written[f"panel:{panel.id}:status"] == "online"
    assert f"panel:{panel.id}:snapshot" in written
