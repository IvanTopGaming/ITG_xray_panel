import json
import os

_HERE = os.path.dirname(__file__)
_CANDIDATES = (
    "/app/versions.json",
    os.path.join(_HERE, "..", "..", "..", "..", "versions.json"),
    os.path.join(_HERE, "..", "..", "..", "..", "..", "versions.json"),
)


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
