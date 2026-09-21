import time

import pytest

from panel_core.models import Client, Inbound, RuntimeApplyState
from panel_core.services import entitlements, runtime_apply, stats
from panel_core.services.entitlements import apply_account_state, reset_source_cycle, revoke_source
from panel_core.services.runtime_identity import build_runtime_email
from panel_core.services.traffic_store import settle_client_traffic
from tests.test_provision_without_restart import grant, live_runtime as live_runtime


@pytest.mark.parametrize("reason", ["quota", "expiry"])
def test_limit_enforcement_keeps_unrelated_sessions(db, live_runtime, reason):
    from panel_core.services.stats import check_limits_and_reset

    result = grant()
    client = db.session.get(Client, result["client"]["id"])
    if reason == "quota":
        client.up = client.limit_bytes
    else:
        client.expiry_time = 1
    db.session.commit()
    live_runtime.calls.clear()
    check_limits_and_reset()
    assert client.enable is False
    assert live_runtime.sessions == {"unrelated-session"}, live_runtime.calls


@pytest.mark.parametrize("action", ["block", "revoke", "reset"])
def test_entitlement_operations_keep_unrelated_sessions(db, live_runtime, action):
    from panel_core.services.entitlements import apply_account_state, revoke_source, reset_source_cycle

    grant()
    live_runtime.calls.clear()
    if action == "block":
        apply_account_state(telegram_id=42, revision=1, blocked=True)
    elif action == "revoke":
        revoke_source(telegram_id=42, inbound_tag="vpn", source_id="pay:1", tariff_id=1)
    else:
        reset_source_cycle(
            telegram_id=42, inbound_tag="vpn", source_id="pay:1", source_revision=0, operation_id="cycle:1", tariff_id=1
        )
    assert live_runtime.sessions == {"unrelated-session"}, live_runtime.calls


def test_expired_legacy_user_gets_access_after_payment(db, live_runtime):
    db.session.add(
        Client(
            id="legacy",
            email="old-user",
            inbound_tag="vpn",
            telegram_id=42,
            tariff_id=1,
            expiry_time=1,
            enable=False,
            disable_reason="expiry",
            limit_bytes=1000,
            up=100,
            down=0,
        )
    )
    db.session.commit()
    result = grant()
    assert result["expires_at_ms"] > int(time.time() * 1000)
    assert result["client"]["enable"] is True


def test_repeated_block_then_unblock_restores_legacy_user(db, live_runtime):
    from panel_core.services.entitlements import apply_account_state

    client = Client(
        id="legacy", email="old-user", inbound_tag="vpn", telegram_id=42, tariff_id=1, expiry_time=0, enable=True
    )
    db.session.add(client)
    db.session.commit()
    apply_account_state(telegram_id=42, revision=1, blocked=True)
    apply_account_state(telegram_id=42, revision=1, blocked=True)
    apply_account_state(telegram_id=42, revision=2, blocked=False)
    assert client.enable is True


def test_expired_source_stops_overriding_remaining_access(db, live_runtime, monkeypatch):
    from datetime import datetime
    from panel_core.services import stats

    end = int(time.time() * 1000) + 100000
    result = grant(
        None, period_ms=None, expiry_ms=0, source_id="grant:permanent", operation_id="grant:permanent", limit_bytes=1000
    )
    grant(
        None,
        period_ms=None,
        expiry_ms=end,
        source_id="grant:temporary",
        operation_id="grant:temporary",
        limit_bytes=100,
    )

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp((end + 1000) / 1000, tz)

    monkeypatch.setattr(stats, "datetime", Later)
    monkeypatch.setattr(entitlements, "_now_ms", lambda: end + 1000)
    stats.check_limits_and_reset()
    client = db.session.get(Client, result["client"]["id"])
    assert client.limit_bytes == 1000


def test_payment_is_not_reported_delivered_with_legacy_access_disabled(db, live_runtime, monkeypatch):
    from panel_core.models import Payment
    from panel_core.services import billing, bot_events

    monkeypatch.setattr(bot_events, "publish_stored", lambda *args: None)
    db.session.add(
        Client(
            id="legacy",
            email="old-user",
            inbound_tag="vpn",
            telegram_id=42,
            tariff_id=1,
            expiry_time=1,
            enable=False,
            disable_reason="",
            limit_bytes=1000,
            up=100,
            down=0,
        )
    )
    payment = Payment(
        id=777,
        yookassa_id="audit-payment",
        telegram_id=42,
        tariff_id=1,
        amount_rub=150,
        status="pending",
        provider_status="succeeded",
        tariff_snapshot={
            "name": "VPN",
            "period_days": 30,
            "items": [{"panel_id": None, "inbound_tag": "vpn", "traffic_gb": 1}],
        },
    )
    db.session.add(payment)
    db.session.commit()
    billing.apply_payment(payment)
    db.session.refresh(payment)
    client = db.session.get(Client, "legacy")
    assert payment.fulfillment_status == "succeeded", payment.fulfillment_error
    assert client.enable is True, (payment.fulfillment_status, client.manual_disabled, client.disable_reason)


@pytest.mark.parametrize(
    "reason,manual,expiry,used,enabled",
    [
        ("", False, 1, 100, True),
        ("expiry", False, 1, 100, True),
        ("quota", False, 0, 1000, True),
        ("", False, 0, 1000, True),
        ("manual", False, 1, 100, False),
        ("", True, 1, 100, False),
        ("", False, 0, 100, False),
        ("invalid", False, 1, 100, False),
    ],
)
def test_legacy_renewal_preserves_only_manual_disables(db, live_runtime, reason, manual, expiry, used, enabled):
    db.session.add(
        Client(
            id="legacy",
            email="old-user",
            inbound_tag="vpn",
            telegram_id=42,
            tariff_id=1,
            expiry_time=expiry,
            enable=False,
            disable_reason=reason,
            manual_disabled=manual,
            limit_bytes=1000,
            up=used,
            down=0,
        )
    )
    db.session.commit()
    result = grant()
    assert result["client"]["enable"] is enabled
    assert bool(live_runtime.users) is enabled


@pytest.mark.parametrize("manual", [False, True])
def test_repeated_block_preserves_original_manual_intent(db, live_runtime, manual):
    client = Client(
        id="legacy",
        email="old-user",
        inbound_tag="vpn",
        telegram_id=42,
        expiry_time=0,
        enable=not manual,
        manual_disabled=manual,
    )
    db.session.add(client)
    db.session.commit()
    apply_account_state(telegram_id=42, revision=1, blocked=True)
    apply_account_state(telegram_id=42, revision=1, blocked=True)
    apply_account_state(telegram_id=42, revision=2, blocked=False)
    apply_account_state(telegram_id=42, revision=2, blocked=False)
    assert client.enable is (not manual)
    assert client.manual_disabled is manual


def test_repeated_revoke_does_not_restart_or_reset_other_usage(db, live_runtime):
    first = grant()
    grant("pay:2")
    revoke = dict(telegram_id=42, inbound_tag="vpn", source_id="pay:1", tariff_id=1)
    revoke_source(**revoke)
    client = db.session.get(Client, first["client"]["id"])
    client.up = 20
    db.session.commit()
    live_runtime.calls.clear()
    revoke_source(**revoke)
    assert client.enable is True
    assert client.up == 20
    assert live_runtime.calls == []


@pytest.mark.parametrize("action", ["block", "revoke", "limits"])
def test_failed_user_removal_still_falls_back_to_full_apply(db, live_runtime, monkeypatch, action):
    grant()
    monkeypatch.setattr(live_runtime, "remove_user", lambda *args: False)
    live_runtime.calls.clear()
    if action == "block":
        apply_account_state(telegram_id=42, revision=1, blocked=True)
    elif action == "revoke":
        revoke_source(telegram_id=42, inbound_tag="vpn", source_id="pay:1", tariff_id=1)
    else:
        Client.query.one().up = 1000
        db.session.commit()
        stats.check_limits_and_reset()
    assert live_runtime.calls == ["restart"]
    assert not live_runtime.users
    state = db.session.get(RuntimeApplyState, 1)
    assert state.desired_revision == state.applied_revision


@pytest.mark.parametrize("routing,protocol", [("special", "vless"), (None, "trojan")])
def test_limit_enforcement_keeps_full_apply_for_routing_or_unsupported_protocol(db, live_runtime, routing, protocol):
    grant()
    client = Client.query.one()
    client.preferred_outbound = routing
    client.up = 1000
    Inbound.query.one().protocol = protocol
    db.session.commit()
    live_runtime.calls.clear()
    stats.check_limits_and_reset()
    assert live_runtime.calls == ["restart"]
    assert not live_runtime.users


def test_limits_recover_an_older_pending_revision(db, live_runtime):
    grant()
    runtime_apply.mark_runtime_dirty()
    db.session.commit()
    live_runtime.calls.clear()
    stats.check_limits_and_reset()
    assert live_runtime.calls == ["restart"]


def test_source_cycle_reenables_quota_without_losing_new_traffic(db, live_runtime):
    grant()
    client = Client.query.one()
    identity = build_runtime_email(client.inbound_tag, client.email)
    counter = f"user>>>{identity}>>>traffic>>>uplink"
    live_runtime.counters[counter] = 1000
    settle_client_traffic(client)
    db.session.commit()
    stats.check_limits_and_reset()
    assert client.enable is False
    live_runtime.calls.clear()
    reset_source_cycle(
        telegram_id=42, inbound_tag="vpn", source_id="pay:1", source_revision=0, operation_id="cycle:1", tariff_id=1
    )
    assert client.enable is True
    assert client.up == 0
    assert live_runtime.calls == ["add"]
    live_runtime.counters[counter] = 1015
    settle_client_traffic(client)
    db.session.commit()
    assert client.up == 15
    assert live_runtime.sessions == {"unrelated-session"}
