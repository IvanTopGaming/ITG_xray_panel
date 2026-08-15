"""A grant can write the expiry it means, instead of adding a period to whatever was there.

The federation contract already carries both semantics: `period_ms` (the node computes
max(now, current) + period) and `expiry_ms` (assign that exact date). Only the second can express an
open-ended grant, and `_validate_provision_semantics` tests both fields with `is None`, so a literal
0 travels as a real value rather than being read as "absent".

No idempotency key is sent alongside `expiry_ms` on purpose: the key exists because adding a period
twice is wrong, while assigning a date twice is not, and a stored receipt would replay the FIRST
date when an admin edits the grant a second time.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from panel_core.models import Tariff, TariffItem
from panel_core.services.provisioning import apply_tariff_for_user


@pytest.fixture
def remote_tariff(app, db):
    def _make(*, traffic_gb: int) -> Tariff:
        tariff = Tariff(name=f"Remote {traffic_gb}", price_rub=0, period_days=30)
        db.session.add(tariff)
        db.session.flush()
        db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="remote-vless", traffic_gb=traffic_gb, panel_id=7))
        db.session.commit()
        return tariff

    return _make


def _payload_of(proxied) -> dict:
    return proxied.call_args.args[3]


def test_open_ended_grant_sends_zero_expiry_and_no_idempotency_key(remote_tariff):
    tariff = remote_tariff(traffic_gb=0)

    with patch("panel_core.services.panel_proxy.proxy_provision") as proxied:
        proxied.return_value = {"expires_at_ms": 0, "client": {}}
        result = apply_tariff_for_user(701, tariff, source="admin_grant", operation_id="grant:test", expiry_ms=0)

    payload = _payload_of(proxied)
    assert payload.get("expiry_ms") == 0, (
        "an open-ended grant must assign expiry 0 -- the node, `evaluate_expiry` and "
        f"`check_limits_and_reset` all read 0 as 'never'; got {payload!r}"
    )
    assert "period_ms" not in payload, (
        f"sending a period as well would make the node add time to a key that has no end; got {payload!r}"
    )
    assert "idempotency_key" not in payload, (
        f"a receipt would replay the first date when the admin edits the grant again; got {payload!r}"
    )
    assert result["expires_at_ms"] == 0, f"the reply must report 'never'; got {result['expires_at_ms']}"


def test_dated_grant_assigns_that_exact_date(remote_tariff):
    tariff = remote_tariff(traffic_gb=10)
    target = 1800000000000

    with patch("panel_core.services.panel_proxy.proxy_provision") as proxied:
        proxied.return_value = {"expires_at_ms": target, "client": {}}
        apply_tariff_for_user(702, tariff, source="admin_grant", operation_id="grant:test", expiry_ms=target)

    payload = _payload_of(proxied)
    assert payload.get("expiry_ms") == target, (
        "the grant's own date must reach the node verbatim -- adding a period instead would give the "
        f"holder a different date than the admin typed; got {payload!r}"
    )
    assert "idempotency_key" not in payload, f"assigning a date is idempotent on its own; got {payload!r}"


def test_omitting_expiry_ms_keeps_the_extend_semantics(remote_tariff):
    tariff = remote_tariff(traffic_gb=300)

    with patch("panel_core.services.panel_proxy.proxy_provision") as proxied:
        proxied.return_value = {"expires_at_ms": 1, "client": {}}
        apply_tariff_for_user(703, tariff, source="pay", operation_id="pay:1")

    payload = _payload_of(proxied)
    assert payload.get("period_ms") == 30 * 86400_000, (
        "a purchase must still extend by a period -- this role holds no Client row for a node-issued "
        f"client, so any expiry it computes is wrong by the remainder it cannot see; got {payload!r}"
    )
    assert payload.get("idempotency_key") == "pay:1", (
        f"extending stays non-idempotent and still needs its key; got {payload!r}"
    )
    assert "expiry_ms" not in payload, f"the contract takes exactly one of the two; got {payload!r}"


def test_an_open_ended_key_produces_no_expiry_warning():
    """The whole point of the change costs no code, so it needs a guard that says why.

    A holder of a free grant received "your access ends in 3 days / 1 day / 1 hour" every cycle,
    because the node saw an ordinary dated key. `evaluate_expiry` returns None for 0 and
    `check_limits_and_reset` only disables a key whose expiry is above 0 -- so the warnings and the
    disconnection both stop by construction. Someone tidying either predicate would bring back a
    monthly false alarm with no test failing.
    """
    from panel_core.services.notifications import evaluate_expiry

    class _Key:
        expiry_time = 0

    assert evaluate_expiry(_Key(), 9_999_999_999_999) is None, (
        "expiry 0 means 'never', so no bucket applies -- returning a bucket here is what sent a free "
        "grant holder an expiry warning every cycle"
    )
