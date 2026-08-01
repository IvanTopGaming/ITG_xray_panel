import os
from urllib.parse import parse_qs, urlparse


def database_uri(sqlite_path):
    env = (os.getenv("DATABASE_URL", "") or "").strip()
    if env:
        return env
    return f"sqlite:///{sqlite_path}"


def is_postgres(uri):
    return uri.startswith("postgresql://") or uri.startswith("postgresql+psycopg2://")


CONNECT_TIMEOUT_SECONDS = 5


def engine_options(uri):
    """`connect_timeout` is the difference between an error and a hang.

    libpq waits indefinitely by default, so a data tier that disappears at the network
    level — the VM down, a partition, a firewall, which is the failure the separate VM
    exists to survive — turned every request into a wait with no answer instead of a fast
    failure. `pool_pre_ping` makes it worse rather than better: it round-trips on every
    checkout, so connections already in the pool hang too.
    """
    if not is_postgres(uri):
        return {}
    return {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_size": 5,
        "max_overflow": 10,
        "connect_args": {"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    }


def validate_database_uri(uri, is_local):
    parsed = urlparse(uri)
    if is_local or not parsed.scheme.startswith("postgresql"):
        return
    query = parse_qs(parsed.query)
    if query.get("sslmode", [""])[0] != "verify-full":
        raise RuntimeError("DATABASE_URL must use sslmode=verify-full for a non-local PANEL_DOMAIN.")
