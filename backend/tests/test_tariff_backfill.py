"""Unit tests for app.services.provisioning.backfill_tariff_item."""

import time as _time
import uuid as _uuid
from unittest.mock import patch

import pytest

from app.models import Client, Inbound, Tariff, TariffItem
from app.services.provisioning import backfill_tariff_item

_GB = 1024**3


@pytest.fixture
def setup(app, db):
    """Two inbounds; tariff already has DE-vless; MSK-vless is the new item."""
    db.session.add_all(
        [
            Inbound(tag="DE-vless", protocol="vless", port=10001, stream_settings="{}"),
            Inbound(tag="MSK-vless", protocol="vless", port=10002, stream_settings="{}"),
        ]
    )
    db.session.flush()
    tariff = Tariff(name="Standard", price_rub=150, period_days=30)
    db.session.add(tariff)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="DE-vless", traffic_gb=0, sort_order=0))
    new_item = TariffItem(tariff_id=tariff.id, inbound_tag="MSK-vless", traffic_gb=70, sort_order=1)
    db.session.add(new_item)
    db.session.commit()
    return tariff, new_item


def _client(db, *, telegram_id, inbound_tag, tariff_id, expiry_ms, enable=True, up=0, down=0):
    c = Client(
        id=str(_uuid.uuid4()),
        email=f"existing_{telegram_id}_{inbound_tag}",
        inbound_tag=inbound_tag,
        telegram_id=telegram_id,
        tariff_id=tariff_id,
        limit_bytes=0,
        expiry_time=expiry_ms,
        up=up,
        down=down,
        enable=enable,
    )
    db.session.add(c)
    db.session.commit()
    return c


def test_creates_key_for_active_holder_inheriting_expiry(app, db, setup):
    tariff, new_item = setup
    now = int(_time.time() * 1000)
    expiry = now + 10 * 86400_000
    _client(db, telegram_id=42, inbound_tag="DE-vless", tariff_id=tariff.id, expiry_ms=expiry)

    with patch("app.services.provisioning._sync_after_provision"):
        created = backfill_tariff_item(tariff, new_item)

    assert created == 1
    new = Client.query.filter_by(telegram_id=42, inbound_tag="MSK-vless").first()
    assert new is not None
    assert new.expiry_time == expiry
    assert new.limit_bytes == 70 * _GB
    assert new.up == 0 and new.down == 0
    assert new.enable is True


def test_skips_holder_who_already_has_key(app, db, setup):
    tariff, new_item = setup
    now = int(_time.time() * 1000)
    _client(db, telegram_id=42, inbound_tag="DE-vless", tariff_id=tariff.id, expiry_ms=now + 86400_000)
    _client(db, telegram_id=42, inbound_tag="MSK-vless", tariff_id=tariff.id, expiry_ms=now + 86400_000)

    with patch("app.services.provisioning._sync_after_provision"):
        created = backfill_tariff_item(tariff, new_item)

    assert created == 0
    assert Client.query.filter_by(telegram_id=42, inbound_tag="MSK-vless").count() == 1


def test_skips_expired_and_disabled_holders(app, db, setup):
    tariff, new_item = setup
    now = int(_time.time() * 1000)
    _client(db, telegram_id=1, inbound_tag="DE-vless", tariff_id=tariff.id, expiry_ms=now - 86400_000)
    _client(db, telegram_id=2, inbound_tag="DE-vless", tariff_id=tariff.id, expiry_ms=now + 86400_000, enable=False)

    with patch("app.services.provisioning._sync_after_provision"):
        created = backfill_tariff_item(tariff, new_item)

    assert created == 0
    assert Client.query.filter_by(inbound_tag="MSK-vless").count() == 0


def test_does_not_touch_existing_keys(app, db, setup):
    tariff, new_item = setup
    now = int(_time.time() * 1000)
    expiry = now + 10 * 86400_000
    existing = _client(
        db, telegram_id=42, inbound_tag="DE-vless", tariff_id=tariff.id, expiry_ms=expiry, up=5_000_000, down=7_000_000
    )

    with patch("app.services.provisioning._sync_after_provision"):
        backfill_tariff_item(tariff, new_item)

    db.session.refresh(existing)
    assert existing.expiry_time == expiry
    assert existing.up == 5_000_000
    assert existing.down == 7_000_000
    assert existing.limit_bytes == 0


def test_unlimited_holder_yields_unlimited_new_key(app, db, setup):
    tariff, new_item = setup
    _client(db, telegram_id=42, inbound_tag="DE-vless", tariff_id=tariff.id, expiry_ms=0)

    with patch("app.services.provisioning._sync_after_provision"):
        backfill_tariff_item(tariff, new_item)

    new = Client.query.filter_by(telegram_id=42, inbound_tag="MSK-vless").first()
    assert new is not None
    assert new.expiry_time == 0


def test_inherits_max_expiry_across_holder_keys(app, db, setup):
    tariff, new_item = setup
    now = int(_time.time() * 1000)
    earlier = now + 3 * 86400_000
    later = now + 20 * 86400_000
    db.session.add_all(
        [
            Inbound(tag="NL-vless", protocol="vless", port=10003, stream_settings="{}"),
        ]
    )
    db.session.flush()
    _client(db, telegram_id=42, inbound_tag="DE-vless", tariff_id=tariff.id, expiry_ms=earlier)
    _client(db, telegram_id=42, inbound_tag="NL-vless", tariff_id=tariff.id, expiry_ms=later)

    with patch("app.services.provisioning._sync_after_provision"):
        backfill_tariff_item(tariff, new_item)

    new = Client.query.filter_by(telegram_id=42, inbound_tag="MSK-vless").first()
    assert new.expiry_time == later
