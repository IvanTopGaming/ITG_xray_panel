import os

import pytest

from tests.schema import ensure_schema

WORKER_BLUEPRINTS = {
    "auth",
    "inbound",
    "outbound",
    "routing",
    "system",
    "statistics",
    "federation",
    "backup",
}
MASTER_BLUEPRINTS = (WORKER_BLUEPRINTS - {"federation", "backup"}) | {"bot_admin", "panels"}
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
EVENT_BUS_JOBS = {
    ("replay_undelivered_bot_events", 60),
    ("cleanup_bot_events", 86400),
}
PAYMENT_JOBS = {
    ("poll_pending_payments", 30),
    ("reconcile_refunds", 3600),
    ("cleanup_old_payments", 86400),
}
WORKER_JOBS = DATA_PLANE_JOBS | DB_MAINTENANCE_JOBS | EVENT_BUS_JOBS
BOT_JOBS = PAYMENT_JOBS
MASTER_JOBS = set()
CRON_JOBS = {
    ("poll_linked_panels", 10),
    ("replay_undelivered_bot_events", 60),
    ("reset_grant_traffic_cycles", 900),
    ("cleanup_bot_events", 86400),
    ("check_latest_version", 21600),
}
CRON_BLUEPRINTS = set()

CASES = [
    ("sub", SUB_BLUEPRINTS, set()),
    ("bot", BOT_BLUEPRINTS, BOT_JOBS),
    ("worker", WORKER_BLUEPRINTS, WORKER_JOBS),
    ("master", MASTER_BLUEPRINTS, MASTER_JOBS),
    ("cron", CRON_BLUEPRINTS, CRON_JOBS),
]


def _reset_scheduler():
    from panel_core.extensions import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


def _build(role, monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/parity-{role}.db"))
    monkeypatch.chdir(tmp_path)

    _reset_scheduler()

    from panel_core.dispatch import create_app

    return create_app()


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


@pytest.mark.parametrize("role", ["sub", "bot", "worker", "master", "cron"])
def test_every_role_serves_health_endpoints(role, monkeypatch, tmp_path):
    app = _build(role, monkeypatch, tmp_path)
    client = app.test_client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code in (200, 503)


def test_default_role_is_master(monkeypatch, tmp_path):
    monkeypatch.delenv("PANEL_ROLE", raising=False)
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/parity-default.db"))
    monkeypatch.chdir(tmp_path)

    _reset_scheduler()

    from panel_core.dispatch import create_app

    app = create_app()
    assert set(app.blueprints) == MASTER_BLUEPRINTS


def test_master_registers_no_data_plane_jobs(monkeypatch, tmp_path):
    _build("master", monkeypatch, tmp_path)
    data_plane_ids = {job_id for job_id, _interval in DATA_PLANE_JOBS}
    assert {job_id for job_id, _interval in _jobs()} & data_plane_ids == set()


def test_the_master_runs_no_scheduler_at_all(monkeypatch, tmp_path):
    _build("master", monkeypatch, tmp_path)
    assert _jobs() == set(), (
        "the master registered a scheduled job. Wave 2 moved every one of them to the cron service so the "
        "master stops being a single point of failure for background work — and so that its -w 1 gunicorn "
        "limit, which exists only because APScheduler is pinned to a web worker, can eventually be lifted. "
        "A job re-added here silently restores both problems."
    )


def test_the_cron_service_serves_no_api(monkeypatch, tmp_path):
    app = _build("cron", monkeypatch, tmp_path)
    api_rules = [r.rule for r in app.url_map.iter_rules() if r.rule.startswith("/api")]
    assert api_rules == [], (
        f"the cron service exposes {api_rules}. It publishes no ports and its host has no Caddy or "
        f"certificate; anything reachable here is reachable by nothing and only widens the image."
    )


def test_worker_registers_every_data_plane_job(monkeypatch, tmp_path):
    _build("worker", monkeypatch, tmp_path)
    assert DATA_PLANE_JOBS <= _jobs()


def test_role_env_is_read_case_insensitively(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "  WORKER  ")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/parity-case.db"))
    monkeypatch.chdir(tmp_path)

    _reset_scheduler()

    from panel_core.dispatch import create_app

    app = create_app()
    assert set(app.blueprints) == WORKER_BLUEPRINTS
    assert os.getenv("PANEL_ROLE").strip().lower() == "worker"
