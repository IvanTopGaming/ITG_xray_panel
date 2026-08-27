"""Live grants become what they already were, and a paused one is not resurrected.

Every `free` grant with a renewal date was being re-provisioned forever, so making it open-ended
takes nothing away from anybody. Its renewal date is already the right first traffic-reset date, so
nothing needs recomputing either.

A grant with no renewal date is paused: the cron stopped renewing it when its tariff was archived,
and the holder's access lapsed weeks or months ago. Handing it back silently during an upgrade is a
surprise nobody asked for, so it remains expired instead of being represented as open-ended access.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from panel_core.models import SystemSetting, Tariff, TariffItem, TelegramUser, UserTariffAccess


@pytest.fixture
def tariffs(app, db):
    def _make(*, name: str, traffic_gb: int, visibility: str = "public") -> Tariff:
        tariff = Tariff(name=name, price_rub=0, period_days=30, visibility=visibility, enabled=True)
        db.session.add(tariff)
        db.session.flush()
        db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="alpha", traffic_gb=traffic_gb, panel_id=2))
        db.session.commit()
        return tariff

    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()
    return _make


def test_a_live_grant_becomes_open_ended_and_its_key_loses_its_date(app, db, tariffs):
    tariff = tariffs(name="Premium", traffic_gb=0)
    db.session.add(
        UserTariffAccess(
            telegram_id=42,
            tariff_id=tariff.id,
            billing="free",
            next_renewal_at=datetime.utcnow() + timedelta(days=20),
        )
    )
    db.session.commit()

    from panel_core.jobs.grant_backfill import backfill_open_ended_grants

    with patch("panel_core.jobs.grant_backfill.apply_tariff_for_user") as applied:
        applied.return_value = {"expires_at_ms": 0, "clients": [], "source": "backfill"}
        converted = backfill_open_ended_grants()

    assert converted == 1, f"the one live grant must be converted; got {converted}"
    assert applied.call_args.kwargs["expiry_ms"] == 0, (
        "the key must lose its date, otherwise the holder keeps receiving the expiry warnings this "
        f"whole change removes; got {applied.call_args.kwargs}"
    )
    grant = UserTariffAccess.query.filter_by(telegram_id=42).first()
    assert grant.access_until is None, "a grant that was renewed forever is open-ended by definition"
    assert grant.next_renewal_at is None, (
        "the tariff is unlimited, so there is no counter to zero and the cron must have no reason to "
        f"touch it; got {grant.next_renewal_at!r}"
    )


def test_a_limited_tariff_keeps_its_reset_date_untouched(app, db, tariffs):
    tariff = tariffs(name="Basic", traffic_gb=300)
    due = datetime.utcnow() + timedelta(days=12)
    db.session.add(UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="free", next_renewal_at=due))
    db.session.commit()

    from panel_core.jobs.grant_backfill import backfill_open_ended_grants

    with patch("panel_core.jobs.grant_backfill.apply_tariff_for_user") as applied:
        applied.return_value = {"expires_at_ms": 0, "clients": [], "source": "backfill"}
        backfill_open_ended_grants()

    grant = UserTariffAccess.query.filter_by(telegram_id=42).first()
    assert grant.next_renewal_at == due, (
        "the date the cron would have renewed on is already the right first traffic reset -- moving "
        f"it would shift every holder's cycle for no reason; got {grant.next_renewal_at!r}"
    )


def test_a_paused_grant_is_left_alone(app, db, tariffs):
    tariff = tariffs(name="Archived", traffic_gb=300, visibility="archived")
    created_at = datetime.utcnow() - timedelta(days=45)
    db.session.add(
        UserTariffAccess(
            telegram_id=42,
            tariff_id=tariff.id,
            billing="free",
            next_renewal_at=None,
            created_at=created_at,
        )
    )
    db.session.commit()

    from panel_core.jobs.grant_backfill import backfill_open_ended_grants

    with patch("panel_core.jobs.grant_backfill.apply_tariff_for_user") as applied:
        converted = backfill_open_ended_grants()

    assert converted == 0, f"a paused grant is not a live one; got {converted}"
    assert not applied.called, (
        "the holder lost access when the tariff was archived; returning it silently during an upgrade "
        "is a surprise nobody asked for"
    )
    grant = UserTariffAccess.query.filter_by(telegram_id=42).one()
    assert grant.access_until == created_at, (
        "a paused legacy row must be made explicitly expired; NULL is the open-ended representation "
        f"and would restore access in the grants UI, got {grant.access_until!r}"
    )


def test_a_paid_grant_is_not_touched(app, db, tariffs):
    tariff = tariffs(name="Private", traffic_gb=0)
    db.session.add(UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="paid"))
    db.session.commit()

    from panel_core.jobs.grant_backfill import backfill_open_ended_grants

    with patch("panel_core.jobs.grant_backfill.apply_tariff_for_user") as applied:
        backfill_open_ended_grants()

    assert not applied.called, "a 'paid' grant issues no key, so there is nothing to make open-ended"


def test_the_backfill_runs_once(app, db, tariffs):
    tariff = tariffs(name="Premium", traffic_gb=0)
    db.session.add(
        UserTariffAccess(
            telegram_id=42,
            tariff_id=tariff.id,
            billing="free",
            next_renewal_at=datetime.utcnow() + timedelta(days=20),
        )
    )
    db.session.commit()

    from panel_core.jobs.grant_backfill import backfill_open_ended_grants

    with patch("panel_core.jobs.grant_backfill.apply_tariff_for_user") as applied:
        applied.return_value = {"expires_at_ms": 0, "clients": [], "source": "backfill"}
        first = backfill_open_ended_grants()
        second = backfill_open_ended_grants()

    assert first == 1 and second == 0, f"a second pass must convert nothing; got {first} then {second}"
    assert applied.call_count == 1, (
        "every restart of the cron service would otherwise re-provision every holder on every node"
    )
    assert SystemSetting.query.filter_by(key="grants_open_ended_backfill").first() is not None, (
        "the run is recorded so a restart does not repeat it"
    )


def test_an_unreachable_node_defers_the_whole_run(app, db, tariffs):
    tariff = tariffs(name="Premium", traffic_gb=0)
    db.session.add(
        UserTariffAccess(
            telegram_id=42,
            tariff_id=tariff.id,
            billing="free",
            next_renewal_at=datetime.utcnow() + timedelta(days=20),
        )
    )
    db.session.commit()

    from panel_core.jobs.grant_backfill import backfill_open_ended_grants

    with patch("panel_core.jobs.grant_backfill.apply_tariff_for_user", side_effect=RuntimeError("panel down")):
        backfill_open_ended_grants()

    assert SystemSetting.query.filter_by(key="grants_open_ended_backfill").first() is None, (
        "recording the run while a node was unreachable would leave that holder dated forever, with "
        "the monthly expiry warnings this change exists to stop and nothing left to retry it"
    )
    grant = UserTariffAccess.query.filter_by(telegram_id=42).first()
    assert grant.next_renewal_at is not None, "a grant whose key was never rewritten must stay as it was"
