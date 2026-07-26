import subprocess
import sys


def test_bootstrap_is_idempotent():
    from panel_core.bootstrap import bootstrap_gevent

    bootstrap_gevent()
    bootstrap_gevent()


def test_bootstrap_installs_the_psycopg_wait_callback():
    from panel_core import pg_compat
    from panel_core.bootstrap import bootstrap_gevent

    pg_compat._patched = False
    bootstrap_gevent()
    assert pg_compat._patched is True


def test_bootstrap_gevent_patches_sockets_when_called_directly_in_a_clean_interpreter():
    probe = (
        "import gevent.monkey;"
        "assert not gevent.monkey.is_module_patched('socket'), 'already patched before bootstrap';"
        "from panel_core.bootstrap import bootstrap_gevent;"
        "bootstrap_gevent();"
        "assert gevent.monkey.is_module_patched('socket'), "
        "'bootstrap_gevent() did not patch sockets when called directly';"
        "print('BOOTSTRAP OK')"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "BOOTSTRAP OK" in result.stdout


def test_the_init_shell_patches_sockets_before_importing_dispatch():
    order_bug_message = (
        "panel_core.dispatch was imported before bootstrap_gevent() patched sockets - the __init__.py "
        "shell must call bootstrap_gevent() before importing dispatch. TEMPORARY: once __init__.py is "
        "deleted in Task 4 and panel_core becomes a namespace package, this test must be replaced with "
        "one that checks the explicit entry points (run.py, the role-dispatch factory) call "
        "bootstrap_gevent() themselves before doing anything socket-sensitive - do not just delete or "
        "weaken this assertion"
    )
    probe = f"""
import builtins
import gevent.monkey

order = []
real_import = builtins.__import__


def traced_import(name, *args, **kwargs):
    if name == "panel_core.dispatch" and not order:
        order.append(gevent.monkey.is_module_patched("socket"))
    return real_import(name, *args, **kwargs)


builtins.__import__ = traced_import
import panel_core

assert order, "panel_core.dispatch was never imported - the trace hook did not fire"
assert order[0] is True, {order_bug_message!r}
print("ORDER OK")
"""
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "ORDER OK" in result.stdout


def test_dispatch_exposes_create_app():
    from panel_core import dispatch

    assert callable(dispatch.create_app)
