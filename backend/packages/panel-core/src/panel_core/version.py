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


def get_app_version(path=None):
    return str(_read_versions(path).get("backend") or "dev")
