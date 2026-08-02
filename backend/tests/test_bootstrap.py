import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tests.schema import ensure_schema

ROLES = ("master", "worker", "sub", "botapi")

CONFTEST = Path(__file__).resolve().parent / "conftest.py"

CONFTEST_BOOTSTRAP_WHY = (
    "tests/conftest.py must call bootstrap_gevent() at module scope, before it imports anything else "
    "from panel_core. panel_core is a namespace package: importing it - or any role module - runs no "
    "code, so nothing patches gevent as a side effect any more. Without the explicit call the suite "
    "imports socket, ssl and threading unpatched and then collapses into a cascade of gevent LoopExit "
    "errors across unrelated tests, which is loud but points nowhere near the cause."
)


def _module_level_imported_names(node):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and not node.level:
        return [node.module or ""]
    return []


def test_conftest_calls_bootstrap_gevent_before_importing_panel_core():
    assert CONFTEST.is_file(), f"conftest.py not found at {CONFTEST} - this guard would pass vacuously"
    body = ast.parse(CONFTEST.read_text()).body

    bootstrap_call = None
    first_panel_core_import = None
    for index, node in enumerate(body):
        if (
            bootstrap_call is None
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "bootstrap_gevent"
        ):
            bootstrap_call = index
        if first_panel_core_import is None:
            for name in _module_level_imported_names(node):
                if (name == "panel_core" or name.startswith("panel_core.")) and name != "panel_core.bootstrap":
                    first_panel_core_import = (index, name)
                    break

    assert bootstrap_call is not None, (
        f"no module-level bootstrap_gevent() call in conftest.py\n\n{CONFTEST_BOOTSTRAP_WHY}"
    )
    assert first_panel_core_import is not None, (
        "conftest.py imports nothing from panel_core, so the ordering assertion below proves nothing. "
        "Re-check what this guard is looking at before relaxing it."
    )
    assert bootstrap_call < first_panel_core_import[0], (
        f"conftest.py imports {first_panel_core_import[1]} at statement {first_panel_core_import[0]}, "
        f"before the bootstrap_gevent() call at statement {bootstrap_call}\n\n{CONFTEST_BOOTSTRAP_WHY}"
    )


def test_the_running_suite_has_patched_sockets():
    import gevent.monkey

    assert gevent.monkey.is_module_patched("socket"), (
        f"the pytest process is running with unpatched sockets\n\n{CONFTEST_BOOTSTRAP_WHY}"
    )


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
    ensure_schema(f"sqlite:///{tmp_path}/run.db")
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
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/{role}.db"))

    pg_compat._patched = False

    import importlib

    module = importlib.import_module(f"panel_core.roles.{role}")
    try:
        module.create_app()

        assert pg_compat._patched is True, (
            f"role {role} built an app without installing the psycopg gevent wait callback; "
            "without it every Postgres query blocks the whole gevent hub"
        )
    finally:
        from panel_core.extensions import scheduler

        scheduler.remove_all_jobs()
        if scheduler.running:
            scheduler.shutdown(wait=False)
