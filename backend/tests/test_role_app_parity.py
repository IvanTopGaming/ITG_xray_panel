import os

import pytest

WORKER_BLUEPRINTS = {
    "auth",
    "inbound",
    "outbound",
    "routing",
    "system",
    "subscription",
    "statistics",
    "federation",
}
MASTER_BLUEPRINTS = WORKER_BLUEPRINTS | {"bot_admin", "bot_service", "billing", "panels"}
SUB_BLUEPRINTS = {"subscription"}
BOT_BLUEPRINTS = {"bot_service", "billing"}

DATA_PLANE_JOBS = {
    ("sync_traffic", 10),
    ("check_limits", 60),
    ("parse_logs", 15),
}
DB_MAINTENANCE_JOBS = {
    ("cleanup_stats", 86400),
}
WORKER_JOBS = DATA_PLANE_JOBS | DB_MAINTENANCE_JOBS
MASTER_JOBS = DB_MAINTENANCE_JOBS | {
    ("auto_renew_free_users", 900),
    ("poll_pending_payments", 30),
    ("reconcile_refunds", 3600),
    ("cleanup_old_payments", 86400),
    ("cleanup_bot_events", 86400),
    ("replay_undelivered_bot_events", 60),
    ("poll_linked_panels", 10),
    ("check_latest_version", 21600),
}

CASES = [
    ("sub", SUB_BLUEPRINTS, set()),
    ("bot", BOT_BLUEPRINTS, set()),
    ("worker", WORKER_BLUEPRINTS, WORKER_JOBS),
    ("master", MASTER_BLUEPRINTS, MASTER_JOBS),
]


def _reset_scheduler():
    from panel_core.extensions import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


def _build(role, monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/parity-{role}.db")
    monkeypatch.chdir(tmp_path)

    _reset_scheduler()

    import panel_core

    return panel_core.create_app()


@pytest.fixture(autouse=True)
def _scheduler_teardown():
    yield
    _reset_scheduler()


def _jobs():
    from panel_core.extensions import scheduler

    return {(job.id, int(job.trigger.interval.total_seconds())) for job in scheduler.get_jobs()}


@pytest.mark.parametrize("role,blueprints,jobs", CASES, ids=[c[0] for c in CASES])
def test_role_registers_exactly_its_blueprints_and_jobs(role, blueprints, jobs, monkeypatch, tmp_path):
    app = _build(role, monkeypatch, tmp_path)
    assert set(app.blueprints) == blueprints
    assert _jobs() == jobs


@pytest.mark.parametrize("role", ["sub", "bot", "worker", "master"])
def test_every_role_serves_health_endpoints(role, monkeypatch, tmp_path):
    app = _build(role, monkeypatch, tmp_path)
    client = app.test_client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code in (200, 503)


def test_default_role_is_master(monkeypatch, tmp_path):
    monkeypatch.delenv("PANEL_ROLE", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/parity-default.db")
    monkeypatch.chdir(tmp_path)

    _reset_scheduler()

    import panel_core

    app = panel_core.create_app()
    assert set(app.blueprints) == MASTER_BLUEPRINTS


def test_master_registers_no_data_plane_jobs(monkeypatch, tmp_path):
    _build("master", monkeypatch, tmp_path)
    data_plane_ids = {job_id for job_id, _interval in DATA_PLANE_JOBS}
    assert {job_id for job_id, _interval in _jobs()} & data_plane_ids == set()


def test_worker_registers_every_data_plane_job(monkeypatch, tmp_path):
    _build("worker", monkeypatch, tmp_path)
    assert DATA_PLANE_JOBS <= _jobs()


def test_role_env_is_read_case_insensitively(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "  WORKER  ")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/parity-case.db")
    monkeypatch.chdir(tmp_path)

    _reset_scheduler()

    import panel_core

    app = panel_core.create_app()
    assert set(app.blueprints) == WORKER_BLUEPRINTS
    assert os.getenv("PANEL_ROLE").strip().lower() == "worker"
