"""Unit tests for app.services.provisioning.backfill_tariff (cross-panel)."""

import time as _time
import uuid as _uuid
from unittest.mock import patch

import pytest

from app.models import Client, Inbound, LinkedPanel, Tariff, TariffItem
from app.services.provisioning import backfill_tariff

_GB = 1024**3


def _snap(clients_by_inbound):
    """{inbound_tag: [client dict, ...]} -> federation snapshot shape."""
    return {"inbounds": [{"tag": tag, "clients": cls} for tag, cls in clients_by_inbound.items()]}


def _cl(telegram_id, tariff_id, *, enable=True, expiry_time=0):
    return {"telegram_id": telegram_id, "tariff_id": tariff_id, "enable": enable, "expiry_time": expiry_time}


@pytest.fixture
def now_ms():
    return int(_time.time() * 1000)


@pytest.fixture
def fed_tariff(app, db):
    """Federation-only tariff: gateway (panel 1) + hiks (panel 2), NO local inbound."""
    db.session.add_all(
        [
            LinkedPanel(id=1, name="gateway", url="https://gw", federation_token="t", status="online", created_at=1),
            LinkedPanel(id=2, name="hiks", url="https://hk", federation_token="t", status="online", created_at=1),
        ]
    )
    t = Tariff(name="Базовый", price_rub=150, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add_all(
        [
            TariffItem(tariff_id=t.id, inbound_tag="gateway", traffic_gb=0, panel_id=1, sort_order=0),
            TariffItem(tariff_id=t.id, inbound_tag="hiks", traffic_gb=70, panel_id=2, sort_order=1),
        ]
    )
    db.session.commit()
    return t


def test_federation_holder_without_local_client_gets_remote_key(app, db, fed_tariff, now_ms):
    """Holder discovered only via the gateway snapshot is provisioned on hiks."""
    expiry = now_ms + 10 * 86400_000
    snaps = {
        1: _snap({"gateway": [_cl(42, fed_tariff.id, expiry_time=expiry)]}),
        2: _snap({"hiks": []}),
    }

    with (
        patch("app.services.provisioning._sync_after_provision"),
        patch("app.services.provisioning.fetch_panel_snapshot_live", side_effect=lambda pid: snaps[pid]),
        patch("app.services.panel_proxy.proxy_provision") as pp,
    ):
        summary = backfill_tariff(fed_tariff)

    assert summary["holders"] == 1
    assert summary["created_remote"] == 1
    assert summary["panels_unreachable"] == []
    pp.assert_called_once()
    args, _ = pp.call_args
    assert args[0] == 2 and args[1] == 42 and args[2] == "hiks"
    assert args[3]["expiry_ms"] == expiry
    assert args[3]["limit_bytes"] == 70 * _GB
    assert args[3]["tariff_id"] == fed_tariff.id


def test_idempotent_skips_holders_who_already_have_remote_key(app, db, fed_tariff, now_ms):
    snaps = {
        1: _snap({"gateway": [_cl(42, fed_tariff.id, expiry_time=now_ms + 86400_000)]}),
        2: _snap({"hiks": [_cl(42, fed_tariff.id, expiry_time=now_ms + 86400_000)]}),
    }
    with (
        patch("app.services.provisioning._sync_after_provision"),
        patch("app.services.provisioning.fetch_panel_snapshot_live", side_effect=lambda pid: snaps[pid]),
        patch("app.services.panel_proxy.proxy_provision") as pp,
    ):
        summary = backfill_tariff(fed_tariff)

    assert summary["created_remote"] == 0
    assert summary["skipped_existing"] >= 1
    pp.assert_not_called()


def test_skips_expired_and_disabled_remote_holders(app, db, fed_tariff, now_ms):
    snaps = {
        1: _snap(
            {
                "gateway": [
                    _cl(1, fed_tariff.id, expiry_time=now_ms - 86400_000),
                    _cl(2, fed_tariff.id, enable=False, expiry_time=now_ms + 86400_000),
                ]
            }
        ),
        2: _snap({"hiks": []}),
    }
    with (
        patch("app.services.provisioning._sync_after_provision"),
        patch("app.services.provisioning.fetch_panel_snapshot_live", side_effect=lambda pid: snaps[pid]),
        patch("app.services.panel_proxy.proxy_provision") as pp,
    ):
        summary = backfill_tariff(fed_tariff)

    assert summary["holders"] == 0
    pp.assert_not_called()


def test_unreachable_panel_reported_and_others_processed(app, db, fed_tariff, now_ms):
    expiry = now_ms + 5 * 86400_000

    def _fetch(pid):
        if pid == 1:
            raise RuntimeError("connection refused")
        return _snap({"hiks": [_cl(99, fed_tariff.id, expiry_time=expiry)]})

    with (
        patch("app.services.provisioning._sync_after_provision"),
        patch("app.services.provisioning.fetch_panel_snapshot_live", side_effect=_fetch),
        patch("app.services.panel_proxy.proxy_provision") as pp,
    ):
        summary = backfill_tariff(fed_tariff)

    assert "gateway" in summary["panels_unreachable"]
    pp.assert_not_called()
    assert summary["created_remote"] == 0


def test_inherits_max_expiry_across_panels_and_unlimited_wins(app, db, fed_tariff, now_ms):
    earlier = now_ms + 3 * 86400_000
    snaps = {
        1: _snap({"gateway": [_cl(7, fed_tariff.id, expiry_time=earlier)]}),
        2: _snap({"hiks": []}),
    }
    with (
        patch("app.services.provisioning._sync_after_provision"),
        patch("app.services.provisioning.fetch_panel_snapshot_live", side_effect=lambda pid: snaps[pid]),
        patch("app.services.panel_proxy.proxy_provision") as pp,
    ):
        backfill_tariff(fed_tariff)
    assert pp.call_args[0][3]["expiry_ms"] == earlier

    snaps[1] = _snap({"gateway": [_cl(7, fed_tariff.id, expiry_time=0)]})
    with (
        patch("app.services.provisioning._sync_after_provision"),
        patch("app.services.provisioning.fetch_panel_snapshot_live", side_effect=lambda pid: snaps[pid]),
        patch("app.services.panel_proxy.proxy_provision") as pp2,
    ):
        backfill_tariff(fed_tariff)
    assert pp2.call_args[0][3]["expiry_ms"] == 0


def test_local_holder_gets_local_key(app, db, now_ms):
    """Mixed tariff: a local inbound + a federation inbound."""
    db.session.add(
        LinkedPanel(id=2, name="hiks", url="https://hk", federation_token="t", status="online", created_at=1)
    )
    db.session.add(Inbound(tag="okins", protocol="vless", port=30001, stream_settings="{}"))
    t = Tariff(name="Премиум", price_rub=300, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add_all(
        [
            TariffItem(tariff_id=t.id, inbound_tag="okins", traffic_gb=0, panel_id=None, sort_order=0),
            TariffItem(tariff_id=t.id, inbound_tag="hiks", traffic_gb=0, panel_id=2, sort_order=1),
        ]
    )
    expiry = now_ms + 10 * 86400_000
    db.session.add(
        Client(
            id=str(_uuid.uuid4()),
            email="local_okins",
            inbound_tag="okins",
            telegram_id=55,
            tariff_id=t.id,
            limit_bytes=0,
            expiry_time=expiry,
            enable=True,
        )
    )
    db.session.commit()

    snaps = {2: _snap({"hiks": []})}
    with (
        patch("app.services.provisioning._sync_after_provision"),
        patch("app.services.provisioning.fetch_panel_snapshot_live", side_effect=lambda pid: snaps[pid]),
        patch("app.services.panel_proxy.proxy_provision") as pp,
    ):
        summary = backfill_tariff(t)

    assert summary["skipped_existing"] >= 1
    pp.assert_called_once()
    assert pp.call_args[0][2] == "hiks"
    assert summary["created_local"] == 0


def test_provision_failure_counted_and_does_not_abort(app, db, fed_tariff, now_ms):
    """A holder is missing the hiks key; proxy_provision raises -> counted in
    provision_failures, batch does not abort, created_remote stays 0."""
    expiry = now_ms + 5 * 86400_000
    snaps = {
        1: _snap({"gateway": [_cl(42, fed_tariff.id, expiry_time=expiry)]}),
        2: _snap({"hiks": []}),
    }
    with (
        patch("app.services.provisioning._sync_after_provision"),
        patch("app.services.provisioning.fetch_panel_snapshot_live", side_effect=lambda pid: snaps[pid]),
        patch("app.services.panel_proxy.proxy_provision", side_effect=RuntimeError("child error")),
    ):
        summary = backfill_tariff(fed_tariff)

    assert summary["provision_failures"] >= 1
    assert summary["created_remote"] == 0
