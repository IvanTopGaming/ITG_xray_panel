def test_patch_gevent_psycopg_is_idempotent_and_installs_callback():
    from panel_core.pg_compat import patch_gevent_psycopg

    patch_gevent_psycopg()
    patch_gevent_psycopg()

    import psycopg2.extensions as ext

    assert ext.get_wait_callback() is not None


def test_patch_gevent_psycopg_no_raise_without_driver(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("psycogreen"):
            raise ImportError("simulated missing psycogreen")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    from importlib import reload
    import panel_core.pg_compat as m

    reload(m)
    m.patch_gevent_psycopg()
