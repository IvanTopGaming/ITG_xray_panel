def bootstrap_gevent() -> None:
    import gevent.monkey

    if not gevent.monkey.is_module_patched("socket"):
        gevent.monkey.patch_all()

    from panel_core.pg_compat import patch_gevent_psycopg

    patch_gevent_psycopg()
