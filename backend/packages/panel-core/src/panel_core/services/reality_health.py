"""§106: the one signal that says a REALITY decoy does not work.

A REALITY inbound can be configured perfectly and still refuse every client, because whether a
given decoy can serve as `dest` is a property of that site rather than of anything the panel can
see. Measured on a live stand: `www.microsoft.com` fails every handshake while `www.google.com`,
`www.bing.com`, `www.apple.com` and `www.cloudflare.com` succeed — and all five serve TLS 1.3, h2,
X25519 and P-256, so no probe of the decoy's advertised properties separates them. Validating the
address at save time therefore cannot work, and the panel used to show a healthy-looking inbound
while nobody could connect.

What does separate them is the node's own error log: a client the server cannot authenticate makes
it log `REALITY: processed invalid connection`. Scans and stale clients produce that too, so a
handful means nothing and the count is reported rather than alarmed on.

**Only the counter lives here.** Reading the log needs the Xray log path, which lives in the
worker's heavy `xray.engine`; the federation snapshot has to publish the number and ships from a
package that must not reach that stack. So the tail is in `panel-worker`'s `stats.py` and this
module is the durable count both sides share.
"""

import json
import logging
import time

from panel_core.extensions import db
from panel_core.models import SystemSetting

SETTING_KEY = "reality_handshake_failures"
WINDOW_SECONDS = 3600

logger = logging.getLogger(__name__)


def _load(now):
    row = SystemSetting.query.filter_by(key=SETTING_KEY).first()
    if row is None or not row.value:
        return row, {"count": 0, "since": now}
    try:
        stored = json.loads(row.value)
        count = int(stored["count"])
        since = float(stored["since"])
    except (ValueError, TypeError, KeyError):
        return row, {"count": 0, "since": now}
    if now - since > WINDOW_SECONDS:
        return row, {"count": 0, "since": now}
    return row, {"count": count, "since": since}


def read_failures(now=None):
    """Answers `{"count": n, "since_ms": ...}`; a window that has aged out reads as zero."""

    moment = time.time() if now is None else now
    try:
        _, state = _load(moment)
    except Exception:
        return {"count": 0, "since_ms": int(moment * 1000)}
    return {"count": state["count"], "since_ms": int(state["since"] * 1000)}


def record_failures(seen, now=None):
    moment = time.time() if now is None else now
    row, state = _load(moment)
    if not seen and row is not None and state["count"] == 0:
        return 0
    state["count"] += int(seen)
    payload = json.dumps({"count": state["count"], "since": state["since"]})
    if row is None:
        db.session.add(SystemSetting(key=SETTING_KEY, value=payload))
    else:
        row.value = payload
    db.session.commit()
    return state["count"]
