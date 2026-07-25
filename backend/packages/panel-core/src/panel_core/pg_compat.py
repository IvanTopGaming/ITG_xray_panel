_patched = False


def patch_gevent_psycopg():
    global _patched
    if _patched:
        return
    try:
        from psycogreen.gevent import patch_psycopg
    except ImportError:
        _patched = True
        return
    patch_psycopg()
    _patched = True
