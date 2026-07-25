import time

_STATE = {"version": None, "reported_at": None}
DEFAULT_FRESHNESS_S = 180


def record_bot_version(version):
    if not version:
        return
    _STATE["version"] = str(version)
    _STATE["reported_at"] = time.time()


def get_bot_status(freshness=DEFAULT_FRESHNESS_S):
    v, at = _STATE["version"], _STATE["reported_at"]
    if v is None or at is None or (time.time() - at) > freshness:
        return {"version": None, "reported_at": None}
    return {"version": v, "reported_at": at}
