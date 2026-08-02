import pytest

BOT_API_JOBS = {
    ("poll_pending_payments", 30),
    ("reconcile_refunds", 3600),
    ("cleanup_old_payments", 86400),
}


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


def test_sub_role_registers_only_subscription(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "sub")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/sub.db")
    monkeypatch.chdir(tmp_path)
    _clear_jobs()

    from panel_core.roles import sub

    app = sub.create_app()
    assert set(app.blueprints) == {"subscription"}


def test_botapi_role_registers_bot_service_and_billing(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "bot")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/bot.db")
    monkeypatch.chdir(tmp_path)
    _clear_jobs()

    from panel_core.roles import botapi

    app = botapi.create_app()
    assert set(app.blueprints) == {"bot_service", "billing"}


def test_sub_role_registers_no_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "sub")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/sub-jobs.db")
    monkeypatch.chdir(tmp_path)
    _clear_jobs()

    from panel_core.roles import sub

    sub.create_app()

    from panel_core.extensions import scheduler

    assert scheduler.get_jobs() == []


def test_botapi_role_registers_payment_jobs_and_starts_the_scheduler(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "bot")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/botapi-jobs.db")
    monkeypatch.chdir(tmp_path)
    _clear_jobs()

    from panel_core.roles import botapi

    botapi.create_app()

    from panel_core.extensions import scheduler

    assert {(job.id, int(job.trigger.interval.total_seconds())) for job in scheduler.get_jobs()} == BOT_API_JOBS
    assert scheduler.running
