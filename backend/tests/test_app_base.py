import importlib

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "build_base_app",
        "ensure_scheduler_job",
        "start_scheduler",
        "run_startup_migration",
        "bootstrap_defaults",
        "db_path",
    ],
)
def test_app_base_exposes(name):
    mod = importlib.import_module("panel_core.app_base")
    assert callable(getattr(mod, name))


def test_base_app_has_health_endpoints_and_no_blueprints(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/base.db")
    monkeypatch.chdir(tmp_path)

    from panel_core.app_base import build_base_app

    app = build_base_app()
    assert app.blueprints == {}
    client = app.test_client()
    assert client.get("/healthz").status_code == 200


def test_base_app_registers_no_scheduler_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/base2.db")
    monkeypatch.chdir(tmp_path)

    from panel_core.extensions import scheduler

    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)

    from panel_core.app_base import build_base_app

    build_base_app()
    assert scheduler.get_jobs() == []
