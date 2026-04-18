import os
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_apscheduler import APScheduler
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
migrate = Migrate()
scheduler = APScheduler()
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=(os.getenv("RATELIMIT_STORAGE_URI", "memory://").strip() or "memory://"),
)

_redis_client_cached = False
_redis_client = None


def get_redis():
    """Lazy: returns a redis.Redis bound to RATELIMIT_STORAGE_URI, or None.

    Reuses the limiter's storage URL on purpose: if the limiter has Redis, the
    subscription cache (and any other consumer) has Redis. No separate env var.
    Errors during construction are swallowed and the cached result becomes None.
    """
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
