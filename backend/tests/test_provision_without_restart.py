import time

import pytest

from panel_core.models import Client, Inbound, ProvisionReceipt, RuntimeApplyState
from panel_core.services import runtime_apply
from panel_core.services.provisioning import provision_single_item
from panel_core.services.runtime_identity import build_runtime_email
from panel_core.services.traffic_store import settle_client_traffic
from panel_core.xray import gateway


class LiveRuntime(gateway.NullXrayGateway):
    def __init__(self):
        self.users = {}
        self.published = {}
        self.sessions = {"unrelated-session"}
        self.calls = []
        self.counters = {}
        self.add_succeeds = True
        self.restart_fails = False
        self.config_fails = False

    def has_local_xray(self):
        return True

    def read_traffic_counters(self, pattern=""):
        return "same-runtime", dict(self.counters)

    def apply_config(self, validate=True, *, publish=True):
        if self.config_fails:
            raise RuntimeError("invalid config")
        if publish:
            self.published = {
                (client.inbound_tag, client.email): client.id for client in Client.query.filter_by(enable=True)
            }

    def add_user(self, tag, client):
        self.calls.append("add")
        key = (tag, client.email)
        if not self.add_succeeds or key in self.users:
            return False
        self.users[key] = client.id
        return True

    def remove_user(self, tag, email):
        self.calls.append("remove")
        self.users.pop((tag, email), None)
        return True

    def restart(self):
        self.calls.append("restart")
        if self.restart_fails:
            raise RuntimeError("restart failed")
        self.sessions.clear()
        self.users = dict(self.published)


@pytest.fixture
def live_runtime(db, monkeypatch):
    runtime = LiveRuntime()
    monkeypatch.setattr(gateway, "_gateway", runtime)
    monkeypatch.setattr(runtime_apply, "restart_xray_container", runtime.restart)
    db.session.add(Inbound(tag="vpn", protocol="vless", port=12345, stream_settings="{}"))
    db.session.commit()
    return runtime


def grant(operation="pay:1", **overrides):
    arguments = {
        "telegram_id": 42,
        "inbound_tag": "vpn",
        "tariff_id": 1,
        "period_ms": 86_400_000,
        "limit_bytes": 1000,
        "idempotency_key": operation,
    }
    arguments.update(overrides)
    return provision_single_item(**arguments)


def test_renewing_a_legacy_client_allows_a_new_expiry_warning(db, live_runtime, monkeypatch):
    from panel_core.services import bot_delivery, bot_events

    monkeypatch.setenv("PANEL_ROLE", "bot")
    client = Client(
        id="legacy-user",
        email="legacy-user",
        inbound_tag="vpn",
        telegram_id=42,
        tariff_id=1,
        expiry_time=int(time.time() * 1000) + 2 * 3600000,
    )
    db.session.add(client)
    db.session.commit()

    def warning():
        event = bot_events.enqueue(
            "expiry_notification",
            42,
            {
                "client_id": client.id,
                "inbound_tag": "vpn",
                "tariff_id": 1,
                "kind": "expiry_1d",
                "access_generation": client.access_generation or "",
                "expiry_time_ms": client.expiry_time,
            },
        )
        db.session.commit()
        verdict = bot_delivery.claim_event(bot_delivery._envelope(event))
        if verdict["claimed"]:
            assert bot_delivery.ack_event(event.source, event.origin_event_id, verdict["lease_token"], "delivered")
        return verdict["claimed"]

    assert warning()
    grant("pay:renewal", period_ms=3600000)
    assert client.access_generation == "pay:renewal"
    assert warning()
    assert not warning()


def assert_applied(db):
    state = db.session.get(RuntimeApplyState, 1)
    assert state.desired_revision == state.applied_revision
    assert state.last_error == ""
    assert all(receipt.materialized for receipt in ProvisionReceipt.query.all())


@pytest.mark.parametrize("protocol", ["vless", "vmess"])
def test_new_user_is_live_without_disconnecting_other_users(db, live_runtime, protocol):
    Inbound.query.filter_by(tag="vpn").one().protocol = protocol
    db.session.commit()

    result = grant()

    assert live_runtime.users == {("vpn", result["client"]["email"]): result["client"]["id"]}
    assert live_runtime.published == live_runtime.users
    assert live_runtime.sessions == {"unrelated-session"}
    assert live_runtime.calls == ["add"]
    assert_applied(db)


def test_renewal_keeps_the_existing_user_and_connections(db, live_runtime):
    first = grant()
    live_runtime.sessions.add("renewing-user-session")
    live_runtime.calls.clear()

    second = grant("pay:2")

    assert second["client"]["id"] == first["client"]["id"]
    assert second["expires_at_ms"] == first["expires_at_ms"] + 86_400_000
    assert live_runtime.sessions == {"unrelated-session", "renewing-user-session"}
    assert live_runtime.calls == []
    assert_applied(db)


@pytest.mark.parametrize("reason", ["expiry", "quota"])
def test_renewal_reenables_an_automatically_disabled_user(db, live_runtime, reason):
    first = grant()
    client = db.session.get(Client, first["client"]["id"])
    client.enable = False
    client.disable_reason = reason
    db.session.commit()
    live_runtime.users.clear()
    live_runtime.calls.clear()

    result = grant("pay:2")

    assert result["client"]["enable"] is True
    assert live_runtime.users == {("vpn", client.email): client.id}
    assert live_runtime.calls == ["add"]
    assert live_runtime.sessions == {"unrelated-session"}
    assert_applied(db)


def test_renewal_preserves_manual_disable_without_restarting(db, live_runtime):
    first = grant()
    client = db.session.get(Client, first["client"]["id"])
    client.enable, client.manual_disabled = False, True
    db.session.commit()
    live_runtime.users.clear()
    live_runtime.calls.clear()

    result = grant("pay:2")

    assert result["client"]["enable"] is False
    assert live_runtime.users == {}
    assert live_runtime.calls == []
    assert live_runtime.sessions == {"unrelated-session"}
    assert_applied(db)


def test_assigning_expired_access_removes_only_that_user(db, live_runtime):
    first = grant()
    live_runtime.calls.clear()

    result = grant(
        None,
        period_ms=None,
        expiry_ms=1,
        source_id="pay:1",
        source_revision=1,
        operation_id="shorten:1",
    )

    assert result["client"]["id"] == first["client"]["id"]
    assert result["client"]["enable"] is False
    assert live_runtime.users == {}
    assert live_runtime.calls == ["remove"]
    assert live_runtime.sessions == {"unrelated-session"}
    assert_applied(db)


def test_replay_does_not_add_a_period_or_touch_runtime(db, live_runtime):
    first = grant()
    live_runtime.calls.clear()

    repeated = grant()

    assert repeated["expires_at_ms"] == first["expires_at_ms"]
    assert live_runtime.calls == []
    assert live_runtime.sessions == {"unrelated-session"}
    assert_applied(db)


def test_grpc_failure_falls_back_to_the_published_config(db, live_runtime):
    live_runtime.add_succeeds = False

    result = grant()

    assert live_runtime.calls == ["add", "restart"]
    assert live_runtime.users == {("vpn", result["client"]["email"]): result["client"]["id"]}
    assert_applied(db)


def test_failed_fallback_is_recovered_without_extending_twice(db, live_runtime):
    live_runtime.add_succeeds = False
    live_runtime.restart_fails = True

    with pytest.raises(runtime_apply.RuntimeApplyError):
        grant()

    receipt = ProvisionReceipt.query.one()
    expiry = Client.query.one().expiry_time
    assert receipt.materialized is False
    state = db.session.get(RuntimeApplyState, 1)
    assert state.desired_revision > state.applied_revision
    live_runtime.restart_fails = False
    live_runtime.calls.clear()

    result = grant()

    assert result["expires_at_ms"] == expiry
    assert live_runtime.calls == ["restart"]
    assert live_runtime.users == live_runtime.published
    assert_applied(db)


def test_unapplied_older_revision_still_requires_full_recovery(db, live_runtime):
    grant()
    runtime_apply.mark_runtime_dirty()
    db.session.commit()
    live_runtime.calls.clear()

    grant("pay:2")

    assert live_runtime.calls == ["restart"]
    assert_applied(db)


@pytest.mark.parametrize("enabled", [False, True])
def test_custom_routing_requires_full_apply_only_when_activation_changes(db, live_runtime, enabled):
    first = grant()
    client = db.session.get(Client, first["client"]["id"])
    client.preferred_outbound = "special-egress"
    client.enable = enabled
    db.session.commit()
    if not enabled:
        live_runtime.users.clear()
    live_runtime.calls.clear()

    grant("pay:2")

    assert live_runtime.calls == ([] if enabled else ["restart"])
    assert live_runtime.users == live_runtime.published
    assert_applied(db)


def test_invalid_config_does_not_commit_a_grant(db, live_runtime):
    live_runtime.config_fails = True

    with pytest.raises(RuntimeError, match="invalid config"):
        grant()

    assert Client.query.count() == 0
    assert ProvisionReceipt.query.count() == 0
    assert live_runtime.users == {}
    assert live_runtime.calls == []


def test_renewal_retains_counter_boundary_without_restarting(db, live_runtime):
    first = grant()
    client = db.session.get(Client, first["client"]["id"])
    identity = build_runtime_email("vpn", client.email)
    counter = f"user>>>{identity}>>>traffic>>>uplink"
    live_runtime.counters[counter] = 100

    grant("pay:2")

    assert client.up == 0
    live_runtime.counters[counter] = 110
    settle_client_traffic(client)
    db.session.commit()
    assert client.up == 10
    assert live_runtime.sessions == {"unrelated-session"}
    assert_applied(db)


def test_unsupported_protocol_keeps_full_apply(db, live_runtime):
    Inbound.query.filter_by(tag="vpn").one().protocol = "trojan"
    db.session.commit()

    result = grant()

    assert result["expires_at_ms"] > int(time.time() * 1000)
    assert live_runtime.calls == ["restart"]
    assert live_runtime.users == live_runtime.published
    assert_applied(db)
