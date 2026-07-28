import json
import logging
import time
import urllib.request

logger = logging.getLogger(__name__)

LATEST_URL = "https://raw.githubusercontent.com/IvanTopGaming/ITG_xray_panel/main/versions.json"

SETTING_KEY = "latest_versions"

_CACHE = {"latest": None, "checked_at": None}


def _http_get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _store(latest, checked_at):
    from panel_core.extensions import db
    from panel_core.models import SystemSetting

    payload = json.dumps({"latest": latest, "checked_at": checked_at})
    row = SystemSetting.query.filter_by(key=SETTING_KEY).first()
    if row is None:
        db.session.add(SystemSetting(key=SETTING_KEY, value=payload))
    else:
        row.value = payload
    db.session.commit()


def _rollback_quietly():
    try:
        from panel_core.extensions import db

        db.session.rollback()
    except Exception:
        pass


def _load():
    from panel_core.models import SystemSetting

    row = SystemSetting.query.filter_by(key=SETTING_KEY).first()
    if row is None or not row.value:
        return None
    stored = json.loads(row.value)
    if not isinstance(stored, dict) or not isinstance(stored.get("latest"), dict):
        return None
    return {"latest": stored["latest"], "checked_at": stored.get("checked_at")}


def fetch_latest():

    try:
        data = _http_get_json(LATEST_URL)
    except Exception as e:  # noqa: BLE001 — best-effort background refresh
        logger.debug("version_check: fetch failed: %s", e)
        return
    if not isinstance(data, dict):
        return

    checked_at = time.time()
    _CACHE["latest"] = data
    _CACHE["checked_at"] = checked_at

    try:
        _store(data, checked_at)
    except Exception as e:  # noqa: BLE001 — the in-process cache still holds the answer
        _rollback_quietly()
        logger.info("version_check: could not persist the result (%s)", e)


def get_latest():

    try:
        stored = _load()
    except Exception as e:  # noqa: BLE001 — no schema, no app context, or a role without the table
        _rollback_quietly()
        logger.debug("version_check: could not read the persisted result (%s)", e)
        stored = None

    if stored is not None:
        return stored
    return {"latest": _CACHE["latest"], "checked_at": _CACHE["checked_at"]}
