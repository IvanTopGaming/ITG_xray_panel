import logging
import os
import time

from flask import g, request

_request_logger = logging.getLogger("app.requests")
_jobs_logger = logging.getLogger("panel_core.jobs")

_SLOW_REQUEST_MS = float(os.getenv("BACKEND_SLOW_REQUEST_MS", "1000"))

_LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"


def setup_logging():
    level_name = (os.getenv("BACKEND_LOG_LEVEL", "INFO") or "INFO").strip().upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level = logging.INFO

    root = logging.getLogger()
    if not getattr(root, "_panel_logging_configured", False):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)
        root._panel_logging_configured = True
    root.setLevel(level)

    if level <= logging.DEBUG:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)
    else:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)


def init_request_logging(app):
    @app.before_request
    def _request_started():
        g._log_t0 = time.monotonic()

    @app.after_request
    def _request_finished(response):
        t0 = getattr(g, "_log_t0", None)
        dur_ms = (time.monotonic() - t0) * 1000 if t0 is not None else -1.0
        path = request.full_path.rstrip("?") if request.query_string else request.path

        if request.path == "/healthz":
            _request_logger.debug("%s %s -> %s in %.0f ms", request.method, path, response.status_code, dur_ms)
            return response

        level = logging.WARNING if dur_ms > _SLOW_REQUEST_MS else logging.INFO
        _request_logger.log(
            level,
            "%s %s -> %s in %.0f ms (%s)",
            request.method,
            path,
            response.status_code,
            dur_ms,
            request.remote_addr,
        )
        return response


def run_job_logged(job_id, interval_seconds, func):
    t0 = time.monotonic()
    _jobs_logger.debug("job %s: start", job_id)
    try:
        func()
    except Exception:
        _jobs_logger.warning("job %s: failed after %.2fs", job_id, time.monotonic() - t0)
        raise
    dur = time.monotonic() - t0
    if dur > interval_seconds:
        _jobs_logger.warning("job %s: overran its %ss interval — done in %.2fs", job_id, interval_seconds, dur)
    else:
        _jobs_logger.info("job %s: done in %.2fs", job_id, dur)
