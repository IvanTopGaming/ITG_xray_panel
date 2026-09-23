import logging

logger = logging.getLogger(__name__)

_patched = False


def patch_gevent_psycopg():
    global _patched
    if _patched:
        return
    try:
        from psycogreen.gevent import patch_psycopg
    except ImportError:
        logger.error("psycogreen is unavailable; PostgreSQL calls will block the worker")
        return
    patch_psycopg()
    _patched = True
