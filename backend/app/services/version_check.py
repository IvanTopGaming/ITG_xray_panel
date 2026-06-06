"""Fetch + cache the latest versions.json from the repo's main branch.

Public repo, no auth. gevent-friendly (urllib with a short timeout). On any
failure the last good value is kept.
"""

import json
import logging
import time
import urllib.request

logger = logging.getLogger(__name__)

LATEST_URL = "https://raw.githubusercontent.com/IvanTopGaming/ITG_xray_panel/main/versions.json"

_CACHE = {"latest": None, "checked_at": None}


def _http_get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_latest():
    """Refresh the cache. Never raises."""
    try:
        data = _http_get_json(LATEST_URL)
        if isinstance(data, dict):
            _CACHE["latest"] = data
            _CACHE["checked_at"] = time.time()
    except Exception as e:  # noqa: BLE001 — best-effort background refresh
        logger.debug("version_check: fetch failed: %s", e)


def get_latest():
    return {"latest": _CACHE["latest"], "checked_at": _CACHE["checked_at"]}
