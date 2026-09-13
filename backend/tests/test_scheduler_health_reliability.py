import pytest
from flask import Flask
from sqlalchemy import text

from panel_core.extensions import db


@pytest.fixture
def job_app(tmp_path):
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'jobs.db'}", PANEL_ROLE="worker")
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.engine.dispose()


def test_registered_never_run_becomes_overdue_and_missing_reading_is_unknown(job_app):
    from panel_core.services.job_status import register_jobs, read_job_status

    assert read_job_status(now_ms=1000)["available"] is False
    register_jobs(job_app, [("sync", 10)], now_ms=1000)
    first = read_job_status(now_ms=1001)
    assert first["items"][0]["status"] == "waiting"
    assert first["items"][0]["last_success_at_ms"] is None
    late = read_job_status(now_ms=100000)
    assert late["items"][0]["status"] == "overdue"
    assert late["needs_attention"] is True


def test_success_and_failure_remain_visible_across_caller_rollback(job_app):
    from panel_core.services.job_status import start_job, finish_job, read_job_status

    run = start_job("sync", 10, now_ms=1000)
    db.session.execute(text("SELECT 1"))
    finish_job("sync", run, error=ValueError("sensitive payload"), now_ms=2000)
    db.session.rollback()
    failed = read_job_status(now_ms=2100)["items"][0]
    assert failed["status"] == "failed"
    assert failed["last_failure_at_ms"] == 2000
    assert failed["last_error"] == "ValueError"
    assert failed["failures"] == 1
    run = start_job("sync", 10, now_ms=3000)
    finish_job("sync", run, now_ms=4000)
    succeeded = read_job_status(now_ms=4100)["items"][0]
    assert succeeded["status"] == "succeeded"
    assert succeeded["last_success_at_ms"] == 4000
    assert succeeded["last_failure_at_ms"] == 2000
    assert succeeded["failures"] == 0


def test_old_run_completion_cannot_replace_newer_run(job_app):
    from panel_core.services.job_status import start_job, finish_job, read_job_status

    first = start_job("sync", 10, now_ms=1000)
    second = start_job("sync", 10, now_ms=2000)
    assert first != second
    assert finish_job("sync", first, error=RuntimeError(), now_ms=3000) is False
    assert read_job_status(now_ms=3001)["items"][0]["status"] == "running"
    assert finish_job("sync", second, now_ms=4000) is True


def test_logged_job_rethrows_original_and_persists_failure_after_dirty_session(job_app, caplog):
    from panel_core.observability import run_job_logged
    from panel_core.services.job_status import read_job_status
    from panel_core.models import SystemSetting

    error = RuntimeError("job failed")

    def fail():
        db.session.add(SystemSetting(key="uncommitted", value="discard"))
        db.session.flush()
        raise error

    with pytest.raises(RuntimeError) as raised:
        run_job_logged("sync", 10, fail)
    assert raised.value is error
    assert db.session.get(SystemSetting, "uncommitted") is None
    assert read_job_status()["items"][0]["status"] == "failed"
    assert any(record.exc_info and "job sync: failed" in record.message for record in caplog.records)


def test_health_sql_failure_is_explicit_and_does_not_touch_caller_session(job_app, monkeypatch):
    from panel_core.services import job_status

    def unavailable(*args, **kwargs):
        raise RuntimeError("database credentials")

    monkeypatch.setattr(job_status, "Session", unavailable)
    reading = job_status.read_job_status()
    assert reading == {"available": False, "error": "Job status unavailable", "items": [], "needs_attention": True}
    assert db.session.execute(text("SELECT 1")).scalar() == 1


def test_wrapper_keeps_its_concrete_app_when_global_scheduler_app_changes(job_app, monkeypatch):
    from types import SimpleNamespace
    from panel_core import app_base
    from panel_core.services.job_status import read_job_status

    scheduled = {}
    fake = SimpleNamespace(app=job_app, get_job=lambda _: None, add_job=lambda **kwargs: scheduled.update(kwargs))
    monkeypatch.setattr(app_base, "scheduler", fake)
    app_base.ensure_scheduler_job("sync", lambda: None, 10)
    other = Flask("other")
    other.config["PANEL_ROLE"] = "cron"
    fake.app = other
    scheduled["func"]()
    row = read_job_status()["items"][0]
    assert row["role"] == "worker"
    assert row["status"] == "succeeded"


def test_scheduler_registers_jobs_before_start(job_app, monkeypatch):
    import datetime as dt
    from types import SimpleNamespace
    from panel_core import app_base
    from panel_core.services.job_status import read_job_status

    observed = []
    fake = SimpleNamespace(
        app=job_app,
        running=False,
        get_jobs=lambda: [SimpleNamespace(id="sync", trigger=SimpleNamespace(interval=dt.timedelta(seconds=10)))],
        start=lambda: observed.append(read_job_status()["items"][0]),
    )
    monkeypatch.setattr(app_base, "scheduler", fake)
    app_base.start_scheduler()
    assert observed[0]["job_id"] == "sync"
    assert observed[0]["status"] == "waiting"
