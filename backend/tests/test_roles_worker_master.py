import pytest


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
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/worker.db")
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
        "subscription",
        "statistics",
        "federation",
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
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/master.db")
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
        "subscription",
        "statistics",
        "federation",
        "bot_admin",
        "panels",
    }
    assert _jobs() == {
        ("cleanup_stats", 86400),
        ("auto_renew_free_users", 900),
        ("cleanup_bot_events", 86400),
        ("replay_undelivered_bot_events", 60),
        ("poll_linked_panels", 10),
        ("check_latest_version", 21600),
    }


def test_worker_does_not_register_master_only_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/worker2.db")
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


def test_panels_job_takes_snapshots_from_traffic_store():
    import ast

    from tests.import_graph import source_path

    panels = source_path("jobs/panels.py")
    tree = ast.parse(panels.read_text())

    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert "panel_core.services.stats" not in modules
    assert "panel_core.services.traffic_store" in modules
