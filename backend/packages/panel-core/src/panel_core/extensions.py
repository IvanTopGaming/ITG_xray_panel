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


CIRCUIT_OPEN_SECONDS = 10


def redis_answered(exc):
    """Did Redis answer, or did the call never reach it?

    A node's credential is `-@all +publish +select &bot:events`, so `sub_cache._delete` and
    `health._data_tier`'s `ping` are refused on every node, by design, several times a minute.
    NOPERM used to arrive through the same `except Exception` as a dead socket and opened the
    breaker for ten seconds, which took `bot:events` down with it — the replay job runs every 60 s
    and kept landing inside a window a refusal had just opened.

    A `ResponseError` is the opposite of an outage: the connection was accepted, TLS completed, the
    credential authenticated and the server replied. Only a call that produced no answer at all may
    open the circuit.
    """

    try:
        import redis  # type: ignore
    except Exception:
        return False
    return isinstance(exc, redis.exceptions.ResponseError)


class _CircuitBreakerRedis:
    """Stops a request paying the socket timeout again once the tier is known to be down.

    Both request-path clients are built with one-second timeouts, which is right for a
    single call and wrong for a whole outage: nothing remembered the failure, so every
    request re-tried every lookup from scratch and a subscription answer that costs 0.5 s
    healthy cost 4-10 s while the data tier was unreachable. Callers already treat a raised
    exception as "no Redis" and fall through, so failing instantly is behaviour-preserving.
    """

    def __init__(self, client, label):
        self._client = client
        self._label = label
        self._down_until = 0.0

    def _open(self):
        was_open = self._down_until > time.monotonic()
        self._down_until = time.monotonic() + CIRCUIT_OPEN_SECONDS
        if not was_open:
            _redis_logger.warning(
                "%s Redis unreachable - skipping it for %ds instead of waiting on every call",
                self._label,
                CIRCUIT_OPEN_SECONDS,
            )

    def _close(self):
        if self._down_until:
            self._down_until = 0.0
            _redis_logger.info("%s Redis recovered", self._label)

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            if time.monotonic() < self._down_until:
                import redis  # type: ignore

                raise redis.exceptions.ConnectionError(
                    f"{self._label} Redis marked unreachable {CIRCUIT_OPEN_SECONDS}s ago; not retrying yet"
                )
            try:
                result = attr(*args, **kwargs)
            except Exception as exc:
                if not redis_answered(exc):
                    self._open()
                raise
            self._close()
            return result

        return call


def _build_redis(uri, circuit_label=None, **kwargs):
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(uri, decode_responses=False, **kwargs)
    except Exception:
        return None
    if circuit_label is None:
        return client
    return _CircuitBreakerRedis(client, circuit_label)


ON_BOX_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def requires_tls(uri):
    """Is this Redis reached across a network somebody else can listen to?

    A bare service name (`redis`) or loopback never leaves the machine, so plain `redis://`
    there is not a weakness — it is the master's and every node's own rate-limit store, and an
    all-in-one deployment reaches the shared tier the same way. Anything with a dotted host is a
    different machine, and the wire between them carries the ACL password, the bot:events payload
    and — the part that decides the severity — node snapshots holding every client's UUID and the
    inbound's REALITY private key. Postgres has demanded `verify-full` in that position since
    wave 1; this is the same rule for the same wire.
    """

    from urllib.parse import urlparse

    if uri.startswith("rediss://"):
        return False
    host = (urlparse(uri).hostname or "").strip().lower()
    if host in ON_BOX_HOSTS or "." not in host:
        return False
    return True


def validate_shared_redis_uri(uri, is_local):

    if is_local or not uri:
        return
    if requires_tls(uri):
        from urllib.parse import urlparse

        host = urlparse(uri).hostname or "?"
        raise RuntimeError(
            f"{SHARED_REDIS_URI_ENV} reaches {host} over plain redis://. That wire carries the ACL "
            f"password, every bot:events payload and the node snapshots — which hold each client's "
            f"UUID and the inbound's REALITY private key — in clear text. Use rediss:// (the data "
            f"tier serves TLS since wave 8), or point this at a Redis on the same machine."
        )


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
    _redis_client = _build_redis(uri, circuit_label="local", socket_connect_timeout=1, socket_timeout=1)
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
    _shared_redis_client = _build_redis(uri, circuit_label="shared", socket_connect_timeout=1, socket_timeout=1)
    return _shared_redis_client


def new_shared_redis_subscriber():
    """A blocking pubsub connection: silence is normal, so it must never time out.

    `socket_timeout=None` is passed explicitly because redis-py 8 changed its own default
    from "block forever" to 5 seconds. Relying on the old default meant a quiet channel
    raised TimeoutError every 5s, the caller treated that as a dropped connection and slept
    before resubscribing, so `panel:refresh` had no subscriber for about half of every
    cycle and lost that share of its messages. `socket_connect_timeout` is looser than the
    request-path clients' 1s on purpose: this connection is long-lived, so a slow connect
    costs nothing while a failed one costs a whole reconnect cycle.
    """
    uri = shared_redis_uri()
    if not uri.startswith(_REDIS_SCHEMES):
        return None
    return _build_redis(uri, socket_connect_timeout=5, socket_timeout=None)
