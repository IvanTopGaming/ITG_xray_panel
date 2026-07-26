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


def test_bootstrap_patches_sockets_in_a_clean_interpreter():
    probe = (
        "import gevent.monkey;"
        "assert not gevent.monkey.is_module_patched('socket'), 'already patched before bootstrap';"
        "from panel_core.bootstrap import bootstrap_gevent;"
        "bootstrap_gevent();"
        "assert gevent.monkey.is_module_patched('socket'), 'bootstrap did not patch sockets';"
        "print('BOOTSTRAP OK')"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "BOOTSTRAP OK" in result.stdout


def test_dispatch_exposes_create_app():
    from panel_core import dispatch

    assert callable(dispatch.create_app)
