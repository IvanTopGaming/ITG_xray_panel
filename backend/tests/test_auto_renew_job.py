from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from panel_core.models import (
    Inbound,
    Tariff,
    TariffItem,
    TelegramUser,
    UserTariffAccess,
)


@pytest.fixture
def basic(app, db):
    db.session.add(Inbound(tag="DE", protocol="vless", port=10001, stream_settings="{}"))
    db.session.flush()
    t = Tariff(name="Free Forever", price_rub=0, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="DE", traffic_gb=10))
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()
    return t


def test_due_grant_is_renewed(app, db, basic):
    from panel_core.jobs.billing import auto_renew_free_users

    tariff = basic
    grant = UserTariffAccess(
        telegram_id=42,
        tariff_id=tariff.id,
        billing="free",
        next_renewal_at=datetime.utcnow() - timedelta(minutes=1),
    )
    db.session.add(grant)
    db.session.commit()

    with patch("panel_core.services.provisioning._sync_after_provision"):
        auto_renew_free_users()

    db.session.refresh(grant)
    assert grant.next_renewal_at > datetime.utcnow()


def test_future_grant_is_skipped(app, db, basic):
    from panel_core.jobs.billing import auto_renew_free_users

    tariff = basic
    future = datetime.utcnow() + timedelta(hours=1)
    grant = UserTariffAccess(
        telegram_id=42,
        tariff_id=tariff.id,
        billing="free",
        next_renewal_at=future,
    )
    db.session.add(grant)
    db.session.commit()

    with patch("panel_core.services.provisioning._sync_after_provision") as mock_sync:
        auto_renew_free_users()

    db.session.refresh(grant)

    assert abs((grant.next_renewal_at - future).total_seconds()) < 1
    mock_sync.assert_not_called()


def test_paid_grant_is_skipped(app, db, basic):
    from panel_core.jobs.billing import auto_renew_free_users

    tariff = basic
    past = datetime.utcnow() - timedelta(hours=1)
    grant = UserTariffAccess(
        telegram_id=42,
        tariff_id=tariff.id,
        billing="paid",
        next_renewal_at=past,
    )
    db.session.add(grant)
    db.session.commit()

    with patch("panel_core.services.provisioning._sync_after_provision") as mock_sync:
        auto_renew_free_users()

    mock_sync.assert_not_called()
    db.session.refresh(grant)
    assert abs((grant.next_renewal_at - past).total_seconds()) < 1


def test_archived_tariff_pauses_grant(app, db, basic):

    from panel_core.jobs.billing import auto_renew_free_users

    tariff = basic
    tariff.visibility = "archived"
    grant = UserTariffAccess(
        telegram_id=42,
        tariff_id=tariff.id,
        billing="free",
        next_renewal_at=datetime.utcnow() - timedelta(hours=1),
    )
    db.session.add(grant)
    db.session.commit()

    with (
        patch("panel_core.services.provisioning._sync_after_provision") as mock_sync,
        patch("panel_core.jobs.billing.bot_events.publish") as mock_publish,
    ):
        auto_renew_free_users()

    db.session.refresh(grant)
    assert grant.next_renewal_at is None
    mock_sync.assert_not_called()

    paused_calls = [c for c in mock_publish.call_args_list if c.args and c.args[0] == "access_paused"]
    assert len(paused_calls) == 1

    call = paused_calls[0]
    assert call.kwargs["telegram_id"] == 42
    payload = call.kwargs["payload"]
    assert payload["tariff_id"] == tariff.id
    assert payload["tariff_name"] == tariff.name
    assert payload["reason"] == "archived"
    assert payload["lang"] == "ru"


def test_disabled_tariff_pauses_with_reason_disabled(app, db, basic):

    from panel_core.jobs.billing import auto_renew_free_users

    tariff = basic
    tariff.enabled = False
    grant = UserTariffAccess(
        telegram_id=42,
        tariff_id=tariff.id,
        billing="free",
        next_renewal_at=datetime.utcnow() - timedelta(hours=1),
    )
    db.session.add(grant)
    db.session.commit()

    with (
        patch("panel_core.services.provisioning._sync_after_provision"),
        patch("panel_core.jobs.billing.bot_events.publish") as mock_publish,
    ):
        auto_renew_free_users()

    paused_calls = [c for c in mock_publish.call_args_list if c.args and c.args[0] == "access_paused"]
    assert len(paused_calls) == 1
    assert paused_calls[0].kwargs["payload"]["reason"] == "disabled"


def test_access_renewed_event_carries_user_lang(app, db, basic):

    from panel_core.jobs.billing import auto_renew_free_users

    tariff = basic
    db.session.add(TelegramUser(telegram_id=777, language="en"))
    db.session.add(
        UserTariffAccess(
            telegram_id=777,
            tariff_id=tariff.id,
            billing="free",
            next_renewal_at=datetime.utcnow() - timedelta(minutes=1),
        )
    )
    db.session.commit()

    with (
        patch("panel_core.services.provisioning._sync_after_provision"),
        patch("panel_core.jobs.billing.bot_events.publish") as mock_publish,
    ):
        auto_renew_free_users()

    renewed = [c for c in mock_publish.call_args_list if c.args and c.args[0] == "access_renewed"]
    assert len(renewed) == 1
    payload = renewed[0].kwargs["payload"]
    assert payload["lang"] == "en"


def test_per_row_error_is_isolated(app, db, basic):

    from panel_core.jobs.billing import auto_renew_free_users

    tariff = basic
    db.session.add(TelegramUser(telegram_id=99, language="ru"))
    g1 = UserTariffAccess(
        telegram_id=42,
        tariff_id=tariff.id,
        billing="free",
        next_renewal_at=datetime.utcnow() - timedelta(hours=1),
    )
    g2 = UserTariffAccess(
        telegram_id=99,
        tariff_id=tariff.id,
        billing="free",
        next_renewal_at=datetime.utcnow() - timedelta(hours=1),
    )
    db.session.add_all([g1, g2])
    db.session.commit()

    call_counter = {"n": 0}

    def fake_sync(*_args, **_kwargs):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            raise RuntimeError("synthetic")

    with patch("panel_core.services.provisioning._sync_after_provision", side_effect=fake_sync):
        auto_renew_free_users()

    db.session.refresh(g1)
    db.session.refresh(g2)
    bumped_count = sum(1 for g in (g1, g2) if g.next_renewal_at and g.next_renewal_at > datetime.utcnow())
    assert bumped_count >= 1
