import json
import os
import pathlib


def _ancestor_candidates():
    here = pathlib.Path(__file__).resolve().parent
    return tuple(str(parent / "versions.json") for parent in here.parents)


_CANDIDATES = ("/app/versions.json",) + _ancestor_candidates()


def _read_versions(path=None):
    paths = (path,) if path else _CANDIDATES
    for p in paths:
        if p and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                continue
    return {}


from panel_core.panel_role import ROLE_BOT, ROLE_MASTER, ROLE_SUB, ROLE_WORKER, current_role

VERSION_KEY_BY_ROLE = {
    ROLE_MASTER: "master",
    ROLE_WORKER: "worker",
    ROLE_SUB: "sub",
    ROLE_BOT: "bot_api",
}


def app_version_key():
    return VERSION_KEY_BY_ROLE[current_role()]


def get_app_version(path=None):
    return str(_read_versions(path).get(app_version_key()) or "dev")
