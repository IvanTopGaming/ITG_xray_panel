import os
import sqlite3

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_apscheduler import APScheduler
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    if not isinstance(dbapi_conn, sqlite3.Connection):
        return
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=15000")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.close()


migrate = Migrate()
scheduler = APScheduler()
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=(os.getenv("RATELIMIT_STORAGE_URI", "memory://").strip() or "memory://"),
)

_redis_client_cached = False
_redis_client = None


def get_redis():

    global _redis_client_cached, _redis_client
    if _redis_client_cached:
        return _redis_client
    _redis_client_cached = True

    uri = (os.getenv("RATELIMIT_STORAGE_URI", "") or "").strip()
    if not (uri.startswith("redis://") or uri.startswith("rediss://")):
        _redis_client = None
        return None
    try:
        import redis  # type: ignore

        _redis_client = redis.Redis.from_url(
            uri,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=False,
        )
    except Exception:
        _redis_client = None
    return _redis_client
