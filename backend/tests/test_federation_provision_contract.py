"""The provisioning contract between an orchestrator and a node.

Every test here builds the app of the role it is talking about. That is not ceremony: the
defect this wave exists to fix (§22 — a renewal reset the remainder instead of extending it)
survived the whole migration precisely because the only extension test built a worker-shaped
app, where the arithmetic already worked. The orchestrator branch had no coverage at all.
"""

import json
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from panel_core.xray import gateway as gw
from panel_core.xray.local import LocalXrayGateway

from tests.schema import ensure_schema

_DAY_MS = 86400_000


def _reset_scheduler():
    from panel_core.extensions import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


@pytest.fixture(autouse=True)
def _scheduler_teardown():
    yield
    _reset_scheduler()


def _build_role(monkeypatch, tmp_path, role, module_name, filename):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/{filename}"))
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    import importlib

    module = importlib.import_module(f"panel_core.roles.{module_name}")
    return module.create_app()


@pytest.fixture
def node_app(monkeypatch, tmp_path):
    return _build_role(monkeypatch, tmp_path, "worker", "worker", "node.db")


@pytest.fixture
def orchestrator_app(monkeypatch, tmp_path):
    return _build_role(monkeypatch, tmp_path, "bot", "botapi", "orchestrator.db")


@contextmanager
def _as_node(node_app):
    """Enter the node's app and its own Xray, the way a real federation call would land."""

    previous = gw.get_xray_gateway()
    with node_app.app_context():
        gw.set_xray_gateway(LocalXrayGateway())
        try:
            with patch("panel_core.services.provisioning._sync_after_provision"):
                yield
        finally:
            gw.set_xray_gateway(previous)


def _seed_node(node_app, *, telegram_id, expiry_ms):
    from panel_core.extensions import db
    from panel_core.models import Client, Inbound

    with node_app.app_context():
        db.session.add(
            Inbound(
                tag="DE-vless",
                port=443,
                protocol="vless",
                stream_settings=json.dumps({"network": "tcp", "security": "reality"}),
            )
        )
        db.session.flush()
        if expiry_ms is not None:
            db.session.add(
                Client(
                    id="node-client",
                    email="tg%s_DE-vless" % telegram_id,
                    inbound_tag="DE-vless",
                    telegram_id=telegram_id,
                    expiry_time=expiry_ms,
                    limit_bytes=0,
                    enable=True,
                )
            )
        db.session.commit()


def _seed_orchestrator(orchestrator_app, *, period_days=30):
    from panel_core.extensions import db
    from panel_core.models import LinkedPanel, Tariff, TariffItem

    with orchestrator_app.app_context():
        panel = LinkedPanel(
            name="node-1",
            url="https://node1.example.com",
            federation_token="tok",
            status="online",
            enable=True,
            created_at=int(time.time() * 1000),
        )
        db.session.add(panel)
        tariff = Tariff(name="Standard", price_rub=150, period_days=period_days)
        db.session.add(tariff)
        db.session.flush()
        db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="DE-vless", traffic_gb=0, panel_id=panel.id))
        db.session.commit()
        return tariff.id


def _wire_federation_to(node_app, calls):
    """Replace the HTTP hop with a direct call into the node's own app.

    This is what makes the test end-to-end: the orchestrator computes nothing about expiry,
    the node does, and the assertion reads the node's database.
    """

    class _DirectFederationClient:
        def __init__(self, url, federation_token):
            pass

        def provision(self, telegram_id, inbound_tag, params):
            calls.append(dict(params))
            from panel_core.services.provisioning import provision_single_item

            with _as_node(node_app):
                return provision_single_item(telegram_id=telegram_id, inbound_tag=inbound_tag, **params)

    return patch("panel_core.services.panel_proxy.FederationClient", _DirectFederationClient)


def _node_expiry(node_app, telegram_id):
    from panel_core.models import Client

    with node_app.app_context():
        return Client.query.filter_by(telegram_id=telegram_id).one().expiry_time


# --------------------------------------------------------------------------------------
# The one-line claim of this wave: a renewal on a node adds to the remainder.
# --------------------------------------------------------------------------------------


def test_a_renewal_on_a_node_adds_to_the_remainder_instead_of_resetting_it(node_app, orchestrator_app):
    from panel_core.services.provisioning import apply_tariff_for_user
    from panel_core.models import Tariff
    from panel_core.extensions import db

    now_ms = int(time.time() * 1000)
    remaining = now_ms + 10 * _DAY_MS
    _seed_node(node_app, telegram_id=777, expiry_ms=remaining)
    tariff_id = _seed_orchestrator(orchestrator_app, period_days=30)

    calls = []
    with orchestrator_app.app_context(), _wire_federation_to(node_app, calls):
        tariff = db.session.get(Tariff, tariff_id)
        result = apply_tariff_for_user(777, tariff, source="yookassa", operation_id="pay:1")

    expiry = _node_expiry(node_app, 777)
    assert expiry == remaining + 30 * _DAY_MS, (
        "the node must add the purchased period to what the user still had; "
        "assigning an absolute date computed by the orchestrator eats the paid remainder, "
        "because the orchestrator's database holds no Client rows for node-issued clients"
    )
    assert result["expires_at_ms"] == expiry, (
        "the date reported back — and shown to the user by the bot — must be the one the node "
        "actually wrote, not the orchestrator's own guess"
    )


def test_the_orchestrator_sends_a_period_and_never_an_absolute_expiry(node_app, orchestrator_app):
    from panel_core.services.provisioning import apply_tariff_for_user
    from panel_core.models import Tariff
    from panel_core.extensions import db

    _seed_node(node_app, telegram_id=778, expiry_ms=int(time.time() * 1000) + 10 * _DAY_MS)
    tariff_id = _seed_orchestrator(orchestrator_app, period_days=30)

    calls = []
    with orchestrator_app.app_context(), _wire_federation_to(node_app, calls):
        tariff = db.session.get(Tariff, tariff_id)
        apply_tariff_for_user(778, tariff, source="yookassa", operation_id="pay:2")

    assert len(calls) == 1
    sent = calls[0]
    assert sent["period_ms"] == 30 * _DAY_MS
    assert "expiry_ms" not in sent, (
        "an orchestrator has no Client rows for node-issued clients, so any expiry it computes "
        "is wrong by exactly the remainder it cannot see"
    )
    assert sent["idempotency_key"] == "pay:2"


def test_a_retried_grant_does_not_add_a_second_period(node_app, orchestrator_app):
    from panel_core.services.provisioning import apply_tariff_for_user
    from panel_core.models import Tariff
    from panel_core.extensions import db

    now_ms = int(time.time() * 1000)
    remaining = now_ms + 10 * _DAY_MS
    _seed_node(node_app, telegram_id=779, expiry_ms=remaining)
    tariff_id = _seed_orchestrator(orchestrator_app, period_days=30)

    calls = []
    with orchestrator_app.app_context(), _wire_federation_to(node_app, calls):
        tariff = db.session.get(Tariff, tariff_id)
        first = apply_tariff_for_user(779, tariff, source="yookassa", operation_id="pay:3")
        second = apply_tariff_for_user(779, tariff, source="yookassa", operation_id="pay:3")

    assert len(calls) == 2, "the retry really did reach the node — the node is what refuses it"
    assert _node_expiry(node_app, 779) == remaining + 30 * _DAY_MS, (
        "the payment poll retries every 30 seconds while a multi-node grant is partially failed, "
        "so without a key the same payment would buy a period on every retry"
    )
    assert second["expires_at_ms"] == first["expires_at_ms"], (
        "the retry must return the stored result: the bot shows the user the date from this reply"
    )


def test_a_different_operation_on_the_same_user_still_adds_a_period(node_app, orchestrator_app):
    from panel_core.services.provisioning import apply_tariff_for_user
    from panel_core.models import Tariff
    from panel_core.extensions import db

    now_ms = int(time.time() * 1000)
    remaining = now_ms + 10 * _DAY_MS
    _seed_node(node_app, telegram_id=780, expiry_ms=remaining)
    tariff_id = _seed_orchestrator(orchestrator_app, period_days=30)

    with orchestrator_app.app_context(), _wire_federation_to(node_app, []):
        tariff = db.session.get(Tariff, tariff_id)
        apply_tariff_for_user(780, tariff, source="yookassa", operation_id="pay:4")
        apply_tariff_for_user(780, tariff, source="yookassa", operation_id="pay:5")

    assert _node_expiry(node_app, 780) == remaining + 60 * _DAY_MS, (
        "the key must suppress a retry of one purchase, not a second genuine purchase"
    )


def test_unlimited_access_survives_buying_a_period_on_top_of_it(node_app, orchestrator_app):
    from panel_core.services.provisioning import apply_tariff_for_user
    from panel_core.models import Tariff
    from panel_core.extensions import db

    _seed_node(node_app, telegram_id=781, expiry_ms=0)
    tariff_id = _seed_orchestrator(orchestrator_app, period_days=30)

    with orchestrator_app.app_context(), _wire_federation_to(node_app, []):
        tariff = db.session.get(Tariff, tariff_id)
        result = apply_tariff_for_user(781, tariff, source="yookassa", operation_id="pay:6")

    assert _node_expiry(node_app, 781) == 0, (
        "expiry_time == 0 means 'never expires' everywhere else in the codebase; adding a period "
        "to it would silently demote unlimited access to a 30-day one"
    )
    assert result["expires_at_ms"] == 0


def test_a_node_that_answers_without_an_expiry_is_refused_loudly(orchestrator_app):
    from panel_core.services.panel_proxy import proxy_provision
    from panel_core.extensions import db
    from panel_core.models import LinkedPanel

    _seed_orchestrator(orchestrator_app)

    class _StaleNode:
        def __init__(self, url, federation_token):
            pass

        def provision(self, telegram_id, inbound_tag, params):
            return {"client": {"id": "x"}, "expires_at_ms": None}

    with orchestrator_app.app_context():
        panel_id = LinkedPanel.query.one().id
        db.session.expire_all()
        with patch("panel_core.services.panel_proxy.FederationClient", _StaleNode):
            with pytest.raises(ValueError) as excinfo:
                proxy_provision(panel_id, 782, "DE-vless", {"period_ms": _DAY_MS, "idempotency_key": "k"})

    message = str(excinfo.value)
    assert "node-1" in message, "the operator must be told which node, not just that something failed"
    assert "update" in message.lower(), (
        "a node left behind on an older release answers a period-based request without an expiry; "
        "treating that reply as success is how §10.5's silent NULL gets written"
    )


# --------------------------------------------------------------------------------------
# The endpoint's own validation, exercised on the role that serves it.
# --------------------------------------------------------------------------------------


def _post_provision(node_app, payload):
    from panel_core.extensions import db
    from panel_core.models import FederationConfig

    with node_app.app_context():
        cfg = db.session.get(FederationConfig, 1)
        if cfg is None:
            cfg = FederationConfig(id=1)
            db.session.add(cfg)
        cfg.federation_token = "fed-token"
        db.session.commit()

    client = node_app.test_client()
    return client.post("/api/federation/provision", json=payload, headers={"X-Federation-Token": "fed-token"})


@pytest.mark.parametrize(
    "extra, expected",
    [
        ({}, "exactly one"),
        ({"period_ms": _DAY_MS, "expiry_ms": 1}, "exactly one"),
        ({"period_ms": _DAY_MS}, "idempotency_key"),
    ],
    ids=["neither-field", "both-fields", "period-without-a-key"],
)
def test_the_endpoint_rejects_a_malformed_contract(node_app, extra, expected):
    _seed_node(node_app, telegram_id=790, expiry_ms=None)

    resp = _post_provision(node_app, {"telegram_id": 790, "inbound_tag": "DE-vless", "limit_bytes": 0, **extra})

    assert resp.status_code == 400
    assert expected in resp.get_json()["error"]


def test_the_endpoint_accepts_an_absolute_expiry_without_a_key(node_app):
    _seed_node(node_app, telegram_id=791, expiry_ms=None)
    target = int(time.time() * 1000) + 5 * _DAY_MS

    with patch("panel_core.services.provisioning._sync_after_provision"):
        resp = _post_provision(
            node_app,
            {"telegram_id": 791, "inbound_tag": "DE-vless", "limit_bytes": 0, "expiry_ms": target},
        )

    assert resp.status_code == 200, (
        "backfill_tariff means 'give this user the same expiry he already has elsewhere', which is "
        "an assignment, not an addition — and an assignment is idempotent on its own"
    )
    assert resp.get_json()["expires_at_ms"] == target
    assert _node_expiry(node_app, 791) == target


def test_backfill_still_sends_an_absolute_expiry(app, db):
    from panel_core.models import Client, Inbound, LinkedPanel, Tariff, TariffItem
    from panel_core.services.provisioning import backfill_tariff

    panel = LinkedPanel(
        name="child",
        url="https://child.example.com",
        federation_token="tok",
        enable=True,
        created_at=int(time.time() * 1000),
    )
    db.session.add(panel)
    db.session.add(Inbound(tag="LOC-vless", protocol="vless", port=10001, stream_settings="{}"))
    tariff = Tariff(name="Fed", price_rub=100, period_days=30)
    db.session.add(tariff)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="LOC-vless", traffic_gb=0, panel_id=None))
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="RMT-vless", traffic_gb=0, panel_id=panel.id))
    held_until = int(time.time() * 1000) + 40 * _DAY_MS
    db.session.add(
        Client(
            id="holder",
            email="tg900_LOC-vless",
            inbound_tag="LOC-vless",
            telegram_id=900,
            tariff_id=tariff.id,
            expiry_time=held_until,
            limit_bytes=0,
            enable=True,
        )
    )
    db.session.commit()

    calls = []

    def _spy(panel_id, telegram_id, inbound_tag, params):
        calls.append(dict(params))
        return {"client": {}, "expires_at_ms": params.get("expiry_ms")}

    with (
        patch("panel_core.services.panel_proxy.proxy_provision", side_effect=_spy),
        patch("panel_core.services.provisioning.fetch_panel_snapshot_live", return_value={"inbounds": []}),
        patch("panel_core.services.provisioning._sync_after_provision"),
    ):
        backfill_tariff(tariff)

    assert calls, "the backfill must have reached the remote item"
    assert calls[0]["expiry_ms"] == held_until, (
        "backfill means 'match the expiry this user already has on his other nodes'; sending a period "
        "instead would give him held_until + period there and drift one tariff's dates apart per node"
    )
    assert "period_ms" not in calls[0]
