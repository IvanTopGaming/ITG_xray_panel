import pytest

from tests.schema import ensure_schema


def _clear_jobs():
    from panel_core.extensions import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


@pytest.fixture(autouse=True)
def _scheduler_teardown():
    yield
    _clear_jobs()


def _jobs():
    from panel_core.extensions import scheduler

    return {(job.id, int(job.trigger.interval.total_seconds())) for job in scheduler.get_jobs()}


def test_worker_role_composition(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/worker.db"))
    monkeypatch.chdir(tmp_path)
    _clear_jobs()

    from panel_core.roles import worker

    app = worker.create_app()
    assert set(app.blueprints) == {
        "auth",
        "inbound",
        "outbound",
        "routing",
        "system",
        "statistics",
        "federation",
        "backup",
    }
    assert _jobs() == {
        ("sync_traffic", 10),
        ("check_limits", 60),
        ("parse_logs", 15),
        ("cleanup_stats", 86400),
        ("replay_undelivered_bot_events", 60),
        ("cleanup_bot_events", 86400),
    }


def test_master_role_composition(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/master.db"))
    monkeypatch.chdir(tmp_path)
    _clear_jobs()

    from panel_core.roles import master

    app = master.create_app()
    assert set(app.blueprints) == {
        "auth",
        "inbound",
        "outbound",
        "routing",
        "system",
        "statistics",
        "bot_admin",
        "panels",
    }
    assert _jobs() == set()


def test_worker_does_not_register_master_only_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/worker2.db"))
    monkeypatch.chdir(tmp_path)
    _clear_jobs()

    from panel_core.roles import worker

    worker.create_app()
    ids = {job_id for job_id, _ in _jobs()}
    assert "poll_linked_panels" not in ids
    assert "poll_pending_payments" not in ids


def test_master_module_does_not_import_stats_or_grpc():
    import ast

    from tests.import_graph import source_path

    master = source_path("roles/master.py")
    tree = ast.parse(master.read_text())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "panel_core.services.stats" not in imported, "master must take cleanup from traffic_store, not the collector"
    assert not any(name.startswith("grpc") for name in imported), (
        "master runs RemoteXrayGateway and makes no gRPC calls; init_gevent() is worker-only"
    )


def test_panels_job_is_a_health_poller_and_ingests_no_traffic():
    """§8.5: poll_linked_panels stopped being a statistics pipeline.

    It fed `node_traffic_snapshot`, a table with writers and zero readers — the master's
    Statistics page reads `traffic_snapshot`/`domain_stat`, which nothing writes into its
    Postgres. Statistics are node-local now, so the transport was removed rather than
    given a consumer. Pinned because the import is the cheapest thing to re-add by reflex.
    """
    import ast

    from tests.import_graph import source_path

    panels = source_path("jobs/panels.py")
    source = panels.read_text()
    tree = ast.parse(source)

    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert "panel_core.services.stats" not in modules
    assert "panel_core.services.traffic_store" not in modules
    assert "_upsert_node_snapshot" not in source
    assert "_diff_snapshots" not in source


def test_master_serves_no_child_side_federation_routes(monkeypatch, tmp_path):
    """§8.2: the master is never a child, so it must not answer the child-side endpoints.

    All five routes in `api/federation.py` are things a *child* offers to its master:
    `link-token` and `config` hand out the pairing token, `handshake` trades it for a
    federation token, and `snapshot`/`provision` let the holder read every client and write
    new ones. The master's own side of federation is `api/panels.py` plus `FederationClient`.

    Not an open door — `handshake` needs a pending `link_token` nobody generated — but a door
    an admin could unlock with one request, after which a stranger holding that token would get
    `snapshot` (inbounds with clients) and `provision` (writes into the master's Postgres).
    """
    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/master-fed.db"))
    monkeypatch.chdir(tmp_path)
    _clear_jobs()

    from panel_core.roles import master

    app = master.create_app()
    rules = {str(rule.rule) for rule in app.url_map.iter_rules()}

    assert not any(rule.startswith("/api/federation/") for rule in rules), (
        f"child-side federation routes leaked onto the master: "
        f"{sorted(r for r in rules if r.startswith('/api/federation/'))}"
    )
    assert "/api/panels" in rules, "the master's own side of federation must stay"
    assert "/api/inbounds" in rules, (
        "inbound CRUD carries admin_or_federation_token_required and is how a node's "
        "federated calls are served — removing the blueprint must not have touched it"
    )
