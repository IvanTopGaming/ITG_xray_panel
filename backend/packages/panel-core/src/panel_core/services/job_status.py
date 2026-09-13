import logging
import time
import uuid

from flask import current_app
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from panel_core.extensions import db
from panel_core.models import JobStatus

logger = logging.getLogger(__name__)


def _now(value):
    return int(time.time() * 1000) if value is None else int(value)


def _ensure(session, role, job_id, interval_s, now):
    if interval_s <= 0:
        raise ValueError("Job interval must be positive")
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    statement = insert(JobStatus).values(
        role=role, job_id=job_id, interval_s=interval_s, registered_at_ms=now, status="waiting", failures=0
    )
    session.execute(statement.on_conflict_do_update(index_elements=["role", "job_id"], set_={"interval_s": interval_s}))


def register_jobs(app, jobs, *, now_ms=None):
    with app.app_context():
        role = current_app.config["PANEL_ROLE"]
        with Session(db.engine) as session, session.begin():
            for job_id, interval_s in jobs:
                _ensure(session, role, job_id, interval_s, _now(now_ms))


def start_job(job_id, interval_s, *, now_ms=None):
    role = current_app.config["PANEL_ROLE"]
    now = _now(now_ms)
    run_id = str(uuid.uuid4())
    with Session(db.engine) as session, session.begin():
        _ensure(session, role, job_id, interval_s, now)
        session.execute(
            update(JobStatus)
            .where(JobStatus.role == role, JobStatus.job_id == job_id)
            .values(started_at_ms=now, run_id=run_id, status="running")
        )
    return run_id


def finish_job(job_id, run_id, *, error=None, now_ms=None):
    role = current_app.config["PANEL_ROLE"]
    now = _now(now_ms)
    values = {"finished_at_ms": now}
    if error is None:
        values.update(status="succeeded", last_success_at_ms=now, failures=0)
    else:
        values.update(
            status="failed",
            last_failure_at_ms=now,
            failures=JobStatus.failures + 1,
            last_error=type(error).__name__[:200],
        )
    with Session(db.engine) as session, session.begin():
        result = session.execute(
            update(JobStatus)
            .where(JobStatus.role == role, JobStatus.job_id == job_id, JobStatus.run_id == run_id)
            .values(**values)
        )
        return result.rowcount == 1


def read_job_status(*, now_ms=None):
    try:
        now = _now(now_ms)
        with Session(db.engine) as session:
            rows = session.scalars(select(JobStatus).order_by(JobStatus.role, JobStatus.job_id)).all()
            items = []
            for row in rows:
                baseline = row.last_success_at_ms if row.last_success_at_ms is not None else row.registered_at_ms
                stale = now - baseline > (row.interval_s + max(30, row.interval_s)) * 1000
                status = "failed" if row.status == "failed" else "overdue" if stale else row.status
                items.append(
                    {
                        "role": row.role,
                        "job_id": row.job_id,
                        "interval_s": row.interval_s,
                        "status": status,
                        "registered_at_ms": row.registered_at_ms,
                        "started_at_ms": row.started_at_ms,
                        "finished_at_ms": row.finished_at_ms,
                        "last_success_at_ms": row.last_success_at_ms,
                        "last_failure_at_ms": row.last_failure_at_ms,
                        "last_error": row.last_error,
                        "failures": row.failures,
                        "stale": stale,
                    }
                )
        if not items:
            return {"available": False, "error": "No registered job readings", "items": [], "needs_attention": True}
        return {
            "available": True,
            "items": items,
            "needs_attention": any(item["stale"] or item["failures"] > 0 for item in items),
        }
    except Exception:
        logger.exception("Job status reading failed")
        return {"available": False, "error": "Job status unavailable", "items": [], "needs_attention": True}
