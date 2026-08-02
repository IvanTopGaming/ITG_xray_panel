import shlex
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_GUNICORN_ENTRY_POINTS = {
    ("docker-compose.master.yml", "backend"),
    ("docker-compose.node.yml", "backend"),
    ("docker-compose.sub.yml", "sub-backend"),
    ("docker-compose.bot.yml", "bot-api"),
    ("docker-compose.cron.yml", "cron"),
}

GEVENT_WORKER_FLAGS = {"-k", "--worker-class"}

WHY = (
    "Nothing in the Python code patches gevent under gunicorn any more. panel_core is a namespace "
    "package, so importing panel_core.roles.<role> runs no module-level code, and bootstrap_gevent() "
    "is called only by run.py (the dev entry point) and tests/conftest.py. In every container the "
    "monkey-patching is done by gunicorn's own worker: GeventWorker.init_process() calls "
    "gevent.monkey.patch_all() before base.Worker.init_process() reaches load_wsgi(). That holds only "
    "while the command carries the gevent worker class and does NOT carry --preload: with --preload "
    "the arbiter calls app.wsgi() (arbiter.py, `if self.cfg.preload_app: self.app.wsgi()`) in the "
    "master process, before any worker has forked or patched, so the whole app graph gets imported "
    "with unpatched sockets and every blocking call then stalls the hub."
)


def _compose_files():
    files = sorted(REPO_ROOT.glob("docker-compose*.yml"))
    assert files, (
        f"no docker-compose*.yml found under {REPO_ROOT} — this guard would pass vacuously. "
        "If the compose files moved, point REPO_ROOT at their new location."
    )
    return files


def _command_tokens(command):
    if isinstance(command, str):
        return shlex.split(command)
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command]
    return []


def _gunicorn_entry_points():
    found = {}
    for path in _compose_files():
        document = yaml.safe_load(path.read_text()) or {}
        for service, definition in (document.get("services") or {}).items():
            if not isinstance(definition, dict):
                continue
            tokens = _command_tokens(definition.get("command"))
            if not any(token.endswith("gunicorn") for token in tokens):
                continue
            found[(path.name, service)] = tokens
    return found


def _worker_classes(tokens):
    values = []
    for index, token in enumerate(tokens):
        if token in GEVENT_WORKER_FLAGS:
            values.extend(tokens[index + 1 : index + 2])
        elif token.split("=")[0] in GEVENT_WORKER_FLAGS and "=" in token:
            values.append(token.split("=", 1)[1])
    return values


def _uses_gevent_worker(tokens):
    values = _worker_classes(tokens)
    return bool(values) and values[-1] == "gevent"


def test_every_declared_gunicorn_entry_point_is_still_found():
    found = set(_gunicorn_entry_points())
    missing = sorted(EXPECTED_GUNICORN_ENTRY_POINTS - found)
    assert missing == [], (
        f"these gunicorn entry points are declared but no longer discoverable: {missing}. Discovery "
        f"found {sorted(found)}. Either the service was renamed/removed (update the declaration) or the "
        "parser stopped recognising its command — and an entry point this guard cannot see is an entry "
        f"point it cannot check.\n\n{WHY}"
    )


@pytest.mark.parametrize("entry_point", sorted(EXPECTED_GUNICORN_ENTRY_POINTS))
def test_gunicorn_entry_point_runs_the_gevent_worker(entry_point):
    tokens = _gunicorn_entry_points().get(entry_point)
    assert tokens is not None, f"gunicorn entry point {entry_point} disappeared"
    assert _uses_gevent_worker(tokens), (
        f"{entry_point[0]} service '{entry_point[1]}' runs gunicorn without the gevent worker class: "
        f"{' '.join(tokens)}\n\n{WHY}"
    )


def test_no_gunicorn_entry_point_preloads_the_app():
    offenders = sorted(
        f"{filename}:{service}"
        for (filename, service), tokens in _gunicorn_entry_points().items()
        if any(token.split("=")[0] == "--preload" for token in tokens)
    )
    assert offenders == [], f"these gunicorn commands preload the app: {offenders}\n\n{WHY}"


def test_no_undeclared_gunicorn_entry_point_escapes_the_check():
    undeclared = sorted(set(_gunicorn_entry_points()) - EXPECTED_GUNICORN_ENTRY_POINTS)
    assert undeclared == [], (
        f"new gunicorn entry points with no entry in EXPECTED_GUNICORN_ENTRY_POINTS: {undeclared}. Add "
        "them deliberately — the parametrised worker-class check only covers what is declared, so a new "
        f"compose file would otherwise ship an unpatched backend unnoticed.\n\n{WHY}"
    )
