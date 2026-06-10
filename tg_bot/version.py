import json
import os

_HERE = os.path.dirname(__file__)
_CANDIDATES = (
    "/app/versions.json",
    os.path.join(_HERE, "versions.json"),
    os.path.join(_HERE, "..", "versions.json"),
)


def get_bot_version():
    for p in _CANDIDATES:
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return str(json.load(f).get("bot") or "dev")
            except (OSError, ValueError):
                continue
    return "dev"
