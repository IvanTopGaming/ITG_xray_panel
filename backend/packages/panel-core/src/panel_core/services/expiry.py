"""One answer to "when does my access end", for every surface that shows it.

A user holds a key per node, and each node computes its own expiry (wave 3a). Every place that
shows a single date therefore folds several into one, and until wave 5b the two halves of the
product folded them differently: the bot took the **latest**, the subscription page and the
`subscription-userinfo` header took the **earliest**. The same account read "30 days" in Telegram
and "3 days" in the client app.

The rule now, everywhere:

- `0` means "never expires" and **absorbs** everything else (§41, customer decision -- buying a
  period on top of unlimited access must not demote it to a dated one, and a summary that says
  "until <date>" for an account that has an unlimited key is wrong in the direction that costs
  the user);
- `None` means "unknown" and is ignored: a damaged row whose `expiry_time` is NULL (§10.5, what a
  node left on a pre-3a release writes) must not be read as unlimited;
- otherwise the **nearest** date wins, because it answers "when do I lose the first thing", and the
  one surface with no per-node breakdown -- the `subscription-userinfo` header a client app reads --
  is exactly the one where an overstated date leaves the user staring at a dead server while the
  app still says three weeks remain.

`backfill_tariff` deliberately does **not** use this: it decides what expiry to *write* on a node a
user does not have a key on yet, not what to show, and there the generous fold is the right one.

The `0` branch below is arithmetically redundant while the fold is `min` -- zero sorts under every
timestamp. It stays because "never expires" is a **meaning**, not a small number, and §42 recorded
the exact trap it guards: a plain `max()` is wrong precisely because unlimited sorts below every
date. Whoever changes the fold reads the rule instead of rediscovering it.
"""

from __future__ import annotations

from collections.abc import Iterable


def nearest_expiry(expiries: Iterable[int | None], *, fallback: int) -> int:

    known = [int(e) for e in expiries if e is not None]
    if not known:
        return fallback
    if 0 in known:
        return 0
    return min(known)
