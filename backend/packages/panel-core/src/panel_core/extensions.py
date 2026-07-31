import logging
import os
import sqlite3
import time

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_apscheduler import APScheduler
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()

_sql_logger = logging.getLogger("app.sql")
_SLOW_SQL_MS = float(os.getenv("BACKEND_SLOW_SQL_MS", "200"))


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    if not isinstance(dbapi_conn, sqlite3.Connection):
        return
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.close()


@event.listens_for(Engine, "before_cursor_execute")
def _sql_timer_start(conn, cursor, statement, parameters, context, executemany):
    conn.info.setdefault("_sql_t0", []).append(time.monotonic())


@event.listens_for(Engine, "after_cursor_execute")
def _sql_timer_stop(conn, cursor, statement, parameters, context, executemany):
    stack = conn.info.get("_sql_t0")
    if not stack:
        return
    dur_ms = (time.monotonic() - stack.pop()) * 1000
    if dur_ms >= _SLOW_SQL_MS:
        _sql_logger.warning(
            "slow SQL %.0f ms%s: %s", dur_ms, " (executemany)" if executemany else "", " ".join(statement.split())[:300]
        )
    elif _sql_logger.isEnabledFor(logging.DEBUG):
        _sql_logger.debug("SQL %.1f ms: %s", dur_ms, " ".join(statement.split())[:300])


migrate = Migrate()
scheduler = APScheduler()
limiter = Limiter(key_func=get_remote_address)

LOCAL_REDIS_URI_ENV = "RATELIMIT_STORAGE_URI"


def local_redis_uri():

    return (os.getenv(LOCAL_REDIS_URI_ENV, "") or "").strip() or "memory://"


SHARED_REDIS_URI_ENV = "SHARED_REDIS_URI"

_REDIS_SCHEMES = ("redis://", "rediss://")

_redis_logger = logging.getLogger("app.redis")

_redis_client_cached = False
_redis_client = None

_shared_redis_cached = False
_shared_redis_client = None


def _build_redis(uri, **kwargs):
    try:
        import redis  # type: ignore

        return redis.Redis.from_url(uri, decode_responses=False, **kwargs)
    except Exception:
        return None


def shared_redis_uri():

    return (os.getenv(SHARED_REDIS_URI_ENV, "") or "").strip()


def reset_redis_clients():

    global _redis_client_cached, _redis_client, _shared_redis_cached, _shared_redis_client
    _redis_client_cached = False
    _redis_client = None
    _shared_redis_cached = False
    _shared_redis_client = None


def get_redis():

    global _redis_client_cached, _redis_client
    if _redis_client_cached:
        return _redis_client
    _redis_client_cached = True

    uri = (os.getenv(LOCAL_REDIS_URI_ENV, "") or "").strip()
    if not uri.startswith(_REDIS_SCHEMES):
        _redis_client = None
        return None
    _redis_client = _build_redis(uri, socket_connect_timeout=1, socket_timeout=1)
    return _redis_client


def get_shared_redis():

    global _shared_redis_cached, _shared_redis_client
    if _shared_redis_cached:
        return _shared_redis_client
    _shared_redis_cached = True

    uri = shared_redis_uri()
    if not uri.startswith(_REDIS_SCHEMES):
        if uri:
            _redis_logger.warning(
                "%s=%r is not a redis:// or rediss:// URI; the shared tier is unreachable from this role",
                SHARED_REDIS_URI_ENV,
                uri,
            )
        _shared_redis_client = None
        return None
    _shared_redis_client = _build_redis(uri, socket_connect_timeout=1, socket_timeout=1)
    return _shared_redis_client


def new_shared_redis_subscriber():

    uri = shared_redis_uri()
    if not uri.startswith(_REDIS_SCHEMES):
        return None
    return _build_redis(uri, socket_connect_timeout=1)
