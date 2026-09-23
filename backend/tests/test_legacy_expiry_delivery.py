import datetime as dt
from types import SimpleNamespace

import pytest

from panel_core.models import BotDelivery, LinkedPanel
from panel_core.services import bot_delivery


@pytest.fixture
def fleet(db, monkeypatch):
    now = dt.datetime(2026, 9, 23, 10, 33)
    now_ms = int(now.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    events = {}
    clients = {}
    for node in (1, 2, 3):
        db.session.add(
            LinkedPanel(
                id=node,
                name=f"node{node}",
                url=f"https://node{node}",
                federation_token="test",
                current_instance_id=f"instance{node}",
                created_at=1,
            )
        )
    db.session.commit()
    monkeypatch.setattr(bot_delivery, "_now", lambda: now)
    monkeypatch.setattr(
        bot_delivery,
        "fetch_panel_snapshot_live",
        lambda node: {"inbounds": [{"tag": "vpn", "clients": [clients[node]]}]},
    )

    def request(self, verb, path, **kwargs):
        if verb == "get":
            return events[self.base_url, int(path.rsplit("/", 1)[1])]
        assert verb == "post" and path.endswith("/ack")
        return {"acked": True}

    monkeypatch.setattr(bot_delivery.FederationClient, "_call_reporting", request)

    def event(node, offset=0, *, generation="", tariff=3, telegram_id=42, kind="expiry_1d", remaining=23 * 3600000):
        expiry = now_ms + remaining + offset
        clients[node] = {
            "id": f"key-{node}",
            "email": f"user-{node}",
            "telegram_id": telegram_id,
            "tariff_id": tariff,
            "access_generation": generation,
            "expiry_time": expiry,
            "last_reset_time": 0,
            "limit_bytes": 0,
            "up": 0,
            "down": 0,
        }
        value = {
            "source": f"node:instance{node}",
            "id": len(events) + 1,
            "type": "expiry_notification",
            "telegram_id": telegram_id,
            "created_at_ms": now_ms - 1000,
            "payload": {
                "client_id": f"key-{node}",
                "inbound_tag": "vpn",
                "tariff_id": tariff,
                "kind": kind,
                "access_generation": generation,
                "expiry_time_ms": expiry,
            },
        }
        events[f"https://node{node}", value["id"]] = value
        return value

    def deliver(value):
        verdict = bot_delivery.claim_event(value)
        if verdict["claimed"]:
            assert bot_delivery.ack_event(value["source"], value["id"], verdict["lease_token"], "delivered")
        return verdict

    return SimpleNamespace(event=event, deliver=deliver, clients=clients)


@pytest.mark.parametrize("offsets", [(0, 936, 329), (1103, 1492, 0), (0, 0, 0)])
def test_legacy_tariff_warning_is_delivered_once_across_nodes(db, fleet, offsets):
    verdicts = [fleet.deliver(fleet.event(node, offset)) for node, offset in enumerate(offsets, 1)]
    assert [verdict["claimed"] for verdict in verdicts] == [True, False, False]
    assert BotDelivery.query.filter_by(state="delivered").count() == 1
    assert BotDelivery.query.filter_by(detail="duplicate_warning").count() == 2


def test_expiry_drift_across_day_boundary_does_not_split_the_warning(db, fleet):
    verdicts = [
        fleet.deliver(fleet.event(node, offset, remaining=13 * 3600000))
        for node, offset in [(1, 1619999), (2, 1620001)]
    ]
    assert [verdict["claimed"] for verdict in verdicts] == [True, False]


def test_renewal_can_deliver_the_same_stage_after_legacy_warning(db, fleet):
    assert fleet.deliver(fleet.event(1))["claimed"]
    assert not fleet.deliver(fleet.event(2, 936))["claimed"]
    assert fleet.deliver(fleet.event(1, generation="pay:renewal"))["claimed"]
    assert not fleet.deliver(fleet.event(2, 936, generation="pay:renewal"))["claimed"]
    assert BotDelivery.query.filter_by(state="delivered").count() == 2


def test_expiry_stages_remain_independent(db, fleet):
    for kind, remaining in [("expiry_3d", 2 * 86400000), ("expiry_1d", 36000000), ("expiry_1h", 1800000), ("expired", -2000)]:
        assert fleet.deliver(fleet.event(1, kind=kind, remaining=remaining))["claimed"]
        assert not fleet.deliver(fleet.event(2, 936, kind=kind, remaining=remaining))["claimed"]
    assert BotDelivery.query.filter_by(state="delivered").count() == 4


@pytest.mark.parametrize("overrides", [{"tariff": 4}, {"telegram_id": 43}])
def test_legacy_warning_does_not_suppress_another_user_or_tariff(db, fleet, overrides):
    assert fleet.deliver(fleet.event(1))["claimed"]
    assert fleet.deliver(fleet.event(2, **overrides))["claimed"]


def test_tariffless_clients_keep_independent_warnings(db, fleet):
    assert fleet.deliver(fleet.event(1, tariff=None))["claimed"]
    assert fleet.deliver(fleet.event(2, tariff=None))["claimed"]


def test_stale_legacy_expiry_cannot_claim_current_tariff_warning(db, fleet):
    stale = fleet.event(1)
    fleet.clients[1]["expiry_time"] += 1
    assert fleet.deliver(stale)["reason"] == "stale_expiry"
    assert fleet.deliver(fleet.event(2, 936))["claimed"]


@pytest.mark.parametrize("state", ["delivered", "pending", "leased"])
def test_pre_upgrade_legacy_claim_still_suppresses_other_nodes(db, fleet, state):
    old = fleet.event(1)
    db.session.add(
        BotDelivery(
            source=old["source"], event_id=old["id"], event=old, dedup_key="a" * 64, state=state, source_acked=True,
            next_attempt_at=bot_delivery._now(),
        )
    )
    db.session.commit()
    assert fleet.deliver(fleet.event(2, 936))["reason"] == "duplicate_warning"


def test_pre_upgrade_retry_keeps_its_own_claim(db, fleet):
    old = fleet.event(1)
    db.session.add(
        BotDelivery(
            source=old["source"], event_id=old["id"], event=old, dedup_key="a" * 64, state="pending", source_acked=True,
            next_attempt_at=bot_delivery._now(),
        )
    )
    db.session.commit()
    assert fleet.deliver(old)["claimed"]
    assert not fleet.deliver(old)["claimed"]


def test_modern_delivery_does_not_consume_legacy_warning(db, fleet):
    assert fleet.deliver(fleet.event(1, generation="pay:other-period"))["claimed"]
    assert fleet.deliver(fleet.event(2))["claimed"]
