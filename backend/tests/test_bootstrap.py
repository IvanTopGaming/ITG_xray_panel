import subprocess
import sys
from pathlib import Path

import pytest

ROLES = ("master", "worker", "sub", "botapi")


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


def test_run_py_patches_sockets_before_importing_dispatch(tmp_path):
    order_bug_message = (
        "panel_core.dispatch was imported before bootstrap_gevent() patched sockets. run.py must call "
        "bootstrap_gevent() on its first lines, before importing anything that touches sockets - "
        "gevent cannot retrofit modules that were already imported unpatched. panel_core is a namespace "
        "package now, so importing it runs no code at all: every entry point owns this ordering itself."
    )
    entry_point = str(Path(__file__).resolve().parents[1] / "run.py")
    probe = f"""
import builtins
import os
import runpy

import gevent.monkey

for k, v in {{"SECRET_KEY": "x" * 40, "PANEL_DOMAIN": "localhost", "PANEL_SECRET_PATH": "/t",
              "PANEL_ADMIN_USER": "admin", "PANEL_ADMIN_PASSWORD": "admin",
              "RATELIMIT_STORAGE_URI": "memory://",
              "DATABASE_URL": "sqlite:///" + {str(tmp_path)!r} + "/run.db"}}.items():
    os.environ.setdefault(k, v)

order = []
real_import = builtins.__import__


def traced_import(name, *args, **kwargs):
    if name == "panel_core.dispatch" and not order:
        order.append(gevent.monkey.is_module_patched("socket"))
    return real_import(name, *args, **kwargs)


builtins.__import__ = traced_import
runpy.run_path({entry_point!r})

assert order, "panel_core.dispatch was never imported by run.py - the trace hook did not fire"
assert order[0] is True, {order_bug_message!r}
print("ORDER OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, cwd=str(tmp_path)
    )
    assert result.returncode == 0, result.stderr
    assert "ORDER OK" in result.stdout


def test_dispatch_exposes_create_app():
    from panel_core import dispatch

    assert callable(dispatch.create_app)


@pytest.mark.parametrize("role", ROLES)
def test_every_role_installs_the_psycopg_wait_callback(role, monkeypatch, tmp_path):
    from panel_core import pg_compat

    monkeypatch.setenv("PANEL_ROLE", "bot" if role == "botapi" else role)
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("PANEL_DOMAIN", "localhost")
    monkeypatch.setenv("PANEL_SECRET_PATH", "/test")
    monkeypatch.setenv("PANEL_ADMIN_USER", "admin")
    monkeypatch.setenv("PANEL_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "memory://")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/{role}.db")

    pg_compat._patched = False

    import importlib

    module = importlib.import_module(f"panel_core.roles.{role}")
    module.create_app()

    assert pg_compat._patched is True, (
        f"role {role} built an app without installing the psycopg gevent wait callback; "
        "without it every Postgres query blocks the whole gevent hub"
    )

    from panel_core.extensions import scheduler

    scheduler.remove_all_jobs()
    if scheduler.running:
        scheduler.shutdown(wait=False)
