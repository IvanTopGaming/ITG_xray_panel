"""§10.8: which version each host is actually running, for the hosts that have no screen.

`versions.json` says what the fleet *should* be on, and System -> About shows what *this* panel is
on. Between those two there is a hole exactly the shape of the three roles with no UI: sub, bot-api
and cron. A host left behind in a lock-step release is otherwise invisible until it does something
wrong, and wave 3a is the worked example of how that ends: an un-updated node writes NULL into an
expiry and stops enforcing limits for everybody it serves.

The mechanism is the one wave 5b built for the bot (§67) and for the same reason -- writer and
reader are different containers, so nothing that lives in a process can carry it. Each of those
roles SETEXes its own version into the shared Redis; the master reads them all. A row that stops
being refreshed disappears rather than going stale, which is the honest answer: this says "the host
is reporting version X", not "the host is healthy".

**Nodes are not here, and that is the correction rather than an omission** (see `ROLE_KEYS`). Wave 6
shipped this claiming to cover them and it could not: a node's data-tier credential forbids `SETEX`,
so the stamp failed silently on every installation, and the key was shared by every node anyway. A
node's version rides its federation snapshot instead -- per panel, already polled, already read.

**Where the refresh comes from.** Every service stack gives its backend a Docker healthcheck against
`/healthz`, so every role receives traffic on its own whether or not a human is looking at it. The
stamp therefore rides on the request path, throttled to once a minute -- no new job, no boot-only
value that outlives the process that wrote it.
"""

import json
import logging
import time

from panel_core.extensions import get_shared_redis

DEFAULT_FRESHNESS_S = 180
STAMP_EVERY_S = 60

# "worker" is deliberately absent, and the reason is two defects rather than one. A node's
# data-tier credential is `-@all +publish +select &bot:events` (wave 2, and that narrowness is what
# makes a node safe in an untrusted segment), so `SETEX` answers NOPERM and the stamp never lands --
# silently, at DEBUG. And the key would be wrong even with the permission: it is `panel:role:worker`
# for *every* node, so three nodes would overwrite each other once a minute and the master would
# show one arbitrary version labelled "worker". A node's version therefore travels in its federation
# snapshot, which is already per-panel and already polled every 10 seconds.
ROLE_KEYS = ("master", "sub", "bot_api", "cron")

logger = logging.getLogger(__name__)

_last_stamp_at = 0.0


def status_key(role_key):
    return f"panel:role:{role_key}:status"


def last_seen_key(role_key):
    return f"panel:role:{role_key}:last"


def reset_stamp_throttle():
    global _last_stamp_at
    _last_stamp_at = 0.0


def record_role_version(role_key, version, *, now=None):
    """Throttled to `STAMP_EVERY_S`; the healthcheck alone would otherwise write every few seconds."""

    global _last_stamp_at
    if not role_key or not version:
        return False
    moment = time.monotonic() if now is None else now
    if _last_stamp_at and (moment - _last_stamp_at) < STAMP_EVERY_S:
        return False
    r = get_shared_redis()
    if r is None:
        return False
    payload = json.dumps({"version": str(version), "reported_at": time.time()}).encode()
    try:
        r.setex(status_key(role_key), DEFAULT_FRESHNESS_S, payload)
        r.set(last_seen_key(role_key), payload)
    except Exception as exc:
        logger.debug("role_status: write to the shared tier failed: %s", exc)
        return False
    _last_stamp_at = moment
    return True


def _decode(raw):
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        version = str(payload["version"])
        reported_at = float(payload["reported_at"])
    except (ValueError, TypeError, KeyError):
        return None
    if not version:
        return None
    return {"version": version, "reported_at": reported_at}


def _read_one(r, role_key, freshness):
    """Fresh if the TTL key is still there, `silent` if only the TTL-less copy is.

    Dropping a role the moment it stops reporting was the wave-6 decision and it is still
    right for the *version* -- a stale number is a claim with no timestamp behind it. But
    absence then carried two meanings at once, "this host was never deployed" and "this
    host died", and for the bot host those are opposite instructions to whoever is looking:
    while it is down no payment is confirmed at all. The TTL-less copy separates them
    without reintroducing the stale claim -- `silent` says when it was last heard from and
    never says it is running that version now.
    """

    try:
        fresh = _decode(r.get(status_key(role_key)))
    except Exception as exc:
        logger.debug("role_status: read for %s failed: %s", role_key, exc)
        return None
    if fresh is not None and (time.time() - fresh["reported_at"]) <= freshness:
        return {**fresh, "state": "reporting"}
    try:
        last = _decode(r.get(last_seen_key(role_key)))
    except Exception as exc:
        logger.debug("role_status: last-seen read for %s failed: %s", role_key, exc)
        return None
    if last is None:
        return None
    return {"version": last["version"], "reported_at": last["reported_at"], "state": "silent"}


def get_role_versions(freshness=DEFAULT_FRESHNESS_S):
    r = get_shared_redis()
    if r is None:
        return {}
    found = {}
    for role_key in ROLE_KEYS:
        entry = _read_one(r, role_key, freshness)
        if entry is not None:
            found[role_key] = entry
    return found
