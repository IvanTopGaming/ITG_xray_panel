from datetime import datetime, timedelta

import pytest

from panel_core.models import LinkedPanel, ProvisionOperation, Tariff, TariffItem, UserTariffAccess


def _tariff(db, name, panel_id=1, traffic_gb=1):
    tariff = Tariff(name=name, price_rub=10, period_days=30)
    db.session.add(tariff)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=tariff.id, panel_id=panel_id, inbound_tag="edge", traffic_gb=traffic_gb))
    db.session.commit()
    return tariff


def test_completed_legacy_grant_is_not_reclassified_after_other_failure(app, db, monkeypatch):
    from panel_core.jobs import grant_backfill

    first, second = _tariff(db, "first", traffic_gb=0), _tariff(db, "second")
    due = datetime.utcnow() + timedelta(days=1)
    good = UserTariffAccess(telegram_id=1, tariff_id=first.id, billing="free", next_renewal_at=due)
    bad = UserTariffAccess(telegram_id=2, tariff_id=second.id, billing="free", next_renewal_at=due)
    db.session.add_all([good, bad])
    db.session.commit()

    def apply(tg, tariff, **kwargs):
        if tg == 2:
            raise RuntimeError("node unavailable")
        return {"expires_at_ms": 0}

    monkeypatch.setattr(grant_backfill, "apply_tariff_for_user", apply)
    assert grant_backfill.backfill_open_ended_grants() == 1
    assert grant_backfill.backfill_open_ended_grants() == 0
    db.session.refresh(good)
    assert good.access_until is None
    assert good.legacy_migration_version == 1


def test_new_open_ended_grant_is_not_treated_as_paused_legacy(app, db, monkeypatch):
    from panel_core.jobs import grant_backfill
    from panel_core.services import grants, provisioning_operations, tariff_targets

    tariff = _tariff(db, "new", traffic_gb=0)
    monkeypatch.setattr(tariff_targets, "validate_tariff_targets", lambda _: None)
    monkeypatch.setattr(
        provisioning_operations, "proxy_provision", lambda *args: {"expires_at_ms": 0, "client": {"id": "fixture"}}
    )
    grant, _ = grants.save_grant(10, tariff, billing="free", access_until=None, silent=True)
    grant_backfill.backfill_open_ended_grants()
    db.session.refresh(grant)
    assert grant.access_until is None
    assert grant.legacy_migration_version == 1


def test_failed_cycle_does_not_advance_its_due_date(app, db, monkeypatch):
    from panel_core.jobs import billing
    from panel_core.services import provisioning_operations

    tariff = _tariff(db, "limited")
    due = datetime.utcnow() - timedelta(minutes=2)
    grant = UserTariffAccess(telegram_id=1, tariff_id=tariff.id, billing="free", next_renewal_at=due)
    db.session.add(grant)
    db.session.commit()
    monkeypatch.setattr(provisioning_operations, "_remote", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    billing.reset_grant_traffic_cycles()
    billing.reset_grant_traffic_cycles()
    db.session.refresh(grant)
    assert grant.next_renewal_at == due
    operations = ProvisionOperation.query.filter_by(kind="reset").all()
    assert len(operations) == 1
    assert operations[0].status == "pending"


def test_null_remote_expiry_is_not_spread_as_unlimited(app, db, monkeypatch):
    from panel_core.services import provisioning

    tariff = _tariff(db, "damaged")
    monkeypatch.setattr(
        provisioning,
        "fetch_panel_snapshot_live",
        lambda _: {
            "inbounds": [
                {
                    "tag": "edge",
                    "clients": [{"telegram_id": 1, "tariff_id": tariff.id, "enable": True, "expiry_time": None}],
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="expiry"):
        provisioning._collect_tariff_holders(tariff, 1000)


def test_active_holder_without_recoverable_source_stays_pending(app, db, monkeypatch):
    from panel_core.services import provisioning

    tariff = _tariff(db, "ambiguous", panel_id=2)
    monkeypatch.setattr(
        provisioning,
        "_collect_tariff_holders",
        lambda *args, **kwargs: ({10: {"expiry_ms": 4102444800000, "have": {(1, "edge")}}}, set()),
    )
    monkeypatch.setattr(provisioning, "_backfill_sources", lambda *args: {})
    operation = provisioning.queue_tariff_backfill(tariff, previous_panel_ids=[1])
    db.session.commit()
    with pytest.raises(ValueError, match="requires_review"):
        provisioning.run_tariff_backfill(operation)
    db.session.refresh(operation)
    assert operation.status == "pending"
    assert operation.last_error


def test_rollout_recovers_child_committed_before_parent_checkpoint(app, db, monkeypatch):
    from panel_core.services import provisioning, provisioning_operations

    tariff = _tariff(db, "checkpoint", panel_id=2)
    monkeypatch.setattr(
        provisioning,
        "_collect_tariff_holders",
        lambda *args, **kwargs: ({10: {"expiry_ms": 4102444800000, "have": set()}}, set()),
    )
    operation = provisioning.queue_tariff_backfill(tariff)
    db.session.commit()
    monkeypatch.setattr(
        provisioning_operations, "proxy_provision", lambda *args: (_ for _ in ()).throw(RuntimeError("down"))
    )
    assert provisioning.run_tariff_backfill(operation)["status"] == "pending"
    child = ProvisionOperation.query.filter_by(source="tariff_backfill").one()
    operation.target_states = {}
    db.session.commit()
    monkeypatch.setattr(
        provisioning, "_collect_tariff_holders", lambda *args, **kwargs: ({10: {"expiry_ms": 0, "have": set()}}, set())
    )
    monkeypatch.setattr(
        provisioning_operations,
        "proxy_provision",
        lambda *args: {"expires_at_ms": 4102444800000, "client": {"id": "fixture"}},
    )
    result = provisioning.run_tariff_backfill(operation)
    assert result["status"] == "succeeded"
    assert ProvisionOperation.query.filter_by(source="tariff_backfill").count() == 1
    assert child.params["expiry_ms"] == 4102444800000


def test_tariff_move_keeps_old_holders_and_retries_failed_target(app, db, monkeypatch):
    from panel_core.services import provisioning, provisioning_operations

    tariff = _tariff(db, "moved", panel_id=2)
    for panel_id in (1, 2):
        db.session.add(
            LinkedPanel(
                id=panel_id,
                name=str(panel_id),
                url=f"https://node{panel_id}.example",
                federation_token="fixture",
                created_at=1,
            )
        )
    db.session.commit()
    expiry = 4102444800000
    monkeypatch.setattr(
        provisioning,
        "fetch_panel_snapshot_live",
        lambda panel_id: {
            "inbounds": [
                {
                    "tag": "edge",
                    "clients": (
                        [{"telegram_id": 1, "tariff_id": tariff.id, "enable": True, "expiry_time": expiry}]
                        if panel_id == 1
                        else []
                    ),
                }
            ]
        },
    )
    attempts = []

    def provision(panel_id, tg, tag, payload):
        attempts.append((panel_id, tg, tag, payload))
        if len(attempts) == 1:
            raise RuntimeError("target down")
        return {"expires_at_ms": expiry, "client": {"id": "key"}}

    monkeypatch.setattr(provisioning_operations, "proxy_provision", provision)
    operation = provisioning.queue_tariff_backfill(tariff, previous_panel_ids=[1])
    db.session.commit()
    first = provisioning.run_tariff_backfill(operation)
    assert first["status"] == "pending"
    second = provisioning.run_tariff_backfill(operation)
    assert second["status"] == "succeeded"
    assert second["holders"] == 1
    assert [call[:3] for call in attempts] == [(2, 1, "edge"), (2, 1, "edge")]
    assert attempts[0][3] == attempts[1][3]


def test_tariff_update_queues_rollout_without_waiting_for_nodes(app, db, monkeypatch):
    from panel_core.api import bot_admin
    from panel_core.services import provisioning

    tariff = _tariff(db, "original", panel_id=1)
    monkeypatch.setattr(bot_admin, "_validate_tariff_payload", lambda payload: None)
    monkeypatch.setattr(
        provisioning, "fetch_panel_snapshot_live", lambda _: pytest.fail("request must not wait for nodes")
    )
    with app.test_request_context(
        json={
            "name": "moved",
            "price_rub": 10,
            "period_days": 30,
            "items": [{"panel_id": 2, "inbound_tag": "edge", "traffic_gb": 1}],
        }
    ):
        response = bot_admin.update_tariff.__wrapped__(tariff.id)
    assert response.status_code == 200
    result = response.get_json()
    assert result["backfill"]["status"] == "pending"
    operation = db.session.get(ProvisionOperation, result["backfill"]["operation_id"])
    assert operation.snapshot["discovery_panel_ids"] == [1]
    assert operation.snapshot["items"][0]["panel_id"] == 2
