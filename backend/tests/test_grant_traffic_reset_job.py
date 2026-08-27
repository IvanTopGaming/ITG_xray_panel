"""The fifteen-minute job stops deciding whether anybody has access and only zeroes counters.

Re-provisioning a key after it had already lapsed is what cost the holder an expiry warning every
cycle and up to fifteen minutes offline per cycle, and what cut every free user off if the cron host
stayed down longer than a tariff period. Access no longer depends on this job running on time: a late
reset just means the counter clears a few minutes later than it could have.

Archiving a tariff no longer pauses a grant either. Taking a tariff out of the catalogue is about
selling it; it must not disconnect the people it was handed to personally.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from panel_core.models import Tariff, TariffItem, TelegramUser, UserTariffAccess


@pytest.fixture
def tariffs(app, db):
    def _make(*, name: str, traffic_gb: int, visibility: str = "public", enabled: bool = True) -> Tariff:
        tariff = Tariff(name=name, price_rub=0, period_days=30, visibility=visibility, enabled=enabled)
        db.session.add(tariff)
        db.session.flush()
        db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="alpha", traffic_gb=traffic_gb, panel_id=2))
        db.session.commit()
        return tariff

    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()
    return _make


def _grant(db, tariff, *, due_minutes_ago: int | None):
    grant = UserTariffAccess(
        telegram_id=42,
        tariff_id=tariff.id,
        billing="free",
        access_until=None,
        next_renewal_at=(datetime.utcnow() - timedelta(minutes=due_minutes_ago)) if due_minutes_ago else None,
    )
    db.session.add(grant)
    db.session.commit()
    return grant


_ONE_REMOTE_CLIENT = {42: [{"panel_id": 2, "inbound_tag": "alpha", "email": "tg42_alpha", "tariff_id": 1}]}


def test_a_due_grant_has_its_counters_zeroed_and_the_date_moved(app, db, tariffs):
    tariff = tariffs(name="Limited", traffic_gb=300)
    _grant(db, tariff, due_minutes_ago=5)

    from panel_core.jobs.billing import reset_grant_traffic_cycles

    with (
        patch("panel_core.jobs.billing.remote_clients_by_telegram_id", return_value=_ONE_REMOTE_CLIENT),
        patch("panel_core.jobs.billing.proxy_bulk_reset_traffic") as reset,
        patch("panel_core.services.provisioning.apply_tariff_for_user") as applied,
    ):
        reset_grant_traffic_cycles()

    assert reset.called, "a due grant on a limited tariff must have its counters zeroed"
    panel_id, users = reset.call_args.args
    assert panel_id == 2 and users == [{"tag": "alpha", "email": "tg42_alpha", "reenable": True}], (
        f"the reset must name the holder's own key on the tariff's own node; got {reset.call_args!r}"
    )
    assert not applied.called, (
        "the job must not provision anything any more -- re-provisioning after the key had lapsed is "
        "what produced the monthly warning and the gap in access"
    )

    grant = UserTariffAccess.query.filter_by(telegram_id=42).first()
    delta = grant.next_renewal_at - datetime.utcnow()
    assert 29 <= delta.days <= 30, f"the next reset moves one tariff period out; got {grant.next_renewal_at!r}"


def test_an_archived_tariff_no_longer_pauses_the_grant(app, db, tariffs):
    tariff = tariffs(name="Gone", traffic_gb=300, visibility="archived")
    _grant(db, tariff, due_minutes_ago=5)

    from panel_core.jobs.billing import reset_grant_traffic_cycles

    with (
        patch("panel_core.jobs.billing.remote_clients_by_telegram_id", return_value=_ONE_REMOTE_CLIENT),
        patch("panel_core.jobs.billing.proxy_bulk_reset_traffic"),
        patch("panel_core.services.bot_events.publish") as published,
    ):
        reset_grant_traffic_cycles()

    grant = UserTariffAccess.query.filter_by(telegram_id=42).first()
    assert grant.next_renewal_at is not None, (
        "archiving takes a tariff out of the catalogue; it must not stop serving the people it was "
        f"already handed to; got {grant.next_renewal_at!r}"
    )
    assert not any(call.args and call.args[0] == "access_paused" for call in published.call_args_list), (
        "'access_paused' announced a disconnection that no longer happens"
    )


def test_a_disabled_tariff_no_longer_pauses_the_grant(app, db, tariffs):
    tariff = tariffs(name="Off", traffic_gb=300, enabled=False)
    _grant(db, tariff, due_minutes_ago=5)

    from panel_core.jobs.billing import reset_grant_traffic_cycles

    with (
        patch("panel_core.jobs.billing.remote_clients_by_telegram_id", return_value=_ONE_REMOTE_CLIENT),
        patch("panel_core.jobs.billing.proxy_bulk_reset_traffic"),
    ):
        reset_grant_traffic_cycles()

    grant = UserTariffAccess.query.filter_by(telegram_id=42).first()
    assert grant.next_renewal_at is not None, (
        f"disabling a tariff hides it from the catalogue and nothing more; got {grant.next_renewal_at!r}"
    )


def test_a_grant_with_no_reset_date_is_left_alone(app, db, tariffs):
    tariff = tariffs(name="Unlimited", traffic_gb=0)
    _grant(db, tariff, due_minutes_ago=None)

    from panel_core.jobs.billing import reset_grant_traffic_cycles

    with patch("panel_core.jobs.billing.proxy_bulk_reset_traffic") as reset:
        reset_grant_traffic_cycles()

    assert not reset.called, "an unlimited tariff has no counter to zero"


def test_a_grant_whose_date_has_not_arrived_is_left_alone(app, db, tariffs):
    tariff = tariffs(name="Limited", traffic_gb=300)
    grant = UserTariffAccess(
        telegram_id=42,
        tariff_id=tariff.id,
        billing="free",
        next_renewal_at=datetime.utcnow() + timedelta(days=10),
    )
    db.session.add(grant)
    db.session.commit()

    from panel_core.jobs.billing import reset_grant_traffic_cycles

    with patch("panel_core.jobs.billing.proxy_bulk_reset_traffic") as reset:
        reset_grant_traffic_cycles()

    assert not reset.called, "the counter is zeroed when the cycle ends, not before"


def test_an_unreachable_node_does_not_stall_the_cycle(app, db, tariffs):
    tariff = tariffs(name="Limited", traffic_gb=300)
    _grant(db, tariff, due_minutes_ago=5)

    from panel_core.jobs.billing import reset_grant_traffic_cycles

    with (
        patch("panel_core.jobs.billing.remote_clients_by_telegram_id", return_value=_ONE_REMOTE_CLIENT),
        patch("panel_core.jobs.billing.proxy_bulk_reset_traffic", side_effect=RuntimeError("panel down")),
    ):
        reset_grant_traffic_cycles()

    grant = UserTariffAccess.query.filter_by(telegram_id=42).first()
    delta = grant.next_renewal_at - datetime.utcnow()
    assert 29 <= delta.days <= 30, (
        "a node that cannot be reached must not leave the grant permanently due -- the job would "
        f"then retry it every fifteen minutes forever; got {grant.next_renewal_at!r}"
    )
