import importlib
import sys
from unittest.mock import patch

import pytest

from grpc.experimental import gevent as grpc_gevent

from tests.call_graph import call_graph
from tests.import_graph import imported_modules, source_path

GRPC_SINKS = {
    "panel_core.xray.grpc_client:get_channel",
    "panel_core.xray.grpc_client:_api_add_user_grpc",
    "panel_core.xray.grpc_client:_api_remove_user_grpc",
}

SUB_ROLE_MODULES = ("panel_core.api.subscription",)
BOT_ROLE_MODULES = ("panel_core.api.bot_service", "panel_core.api.billing")

XRAY_REACHING_HANDLERS = {
    "panel_core.api.bot_service:activate_trial",
    "panel_core.api.billing:yookassa_webhook",
}

FAILURE_HINT = (
    "A light-role endpoint gained a call path to Xray gRPC. These two reach it only through "
    "provisioning's local-inbound branch, which _require_local_xray() refuses on a role without a "
    "local Xray; any new one must be equally unreachable at runtime, or the role must call "
    "grpc_gevent.init_gevent() in its factory before the call can block the gevent hub."
)


def _reaching(*modules):
    graph = call_graph()
    reaching = {}
    for handler in graph.route_handlers(*modules):
        path = graph.path_to(handler, GRPC_SINKS)
        if path is not None:
            reaching[handler] = " -> ".join(graph.describe(step) for step in path)
    return reaching


def test_sub_role_endpoints_cannot_reach_xray_grpc():
    assert call_graph().route_handlers(*SUB_ROLE_MODULES)
    assert _reaching(*SUB_ROLE_MODULES) == {}, FAILURE_HINT


def test_only_provisioning_bot_endpoints_can_reach_xray_grpc():
    reaching = _reaching(*BOT_ROLE_MODULES)
    assert set(reaching) == XRAY_REACHING_HANDLERS, FAILURE_HINT + f"\nreaching: {reaching}"


HEAVY_ROLES = ("worker",)
LIGHT_ROLES = ("sub", "botapi", "master")

INIT_GEVENT_HINT = (
    "Without grpc_gevent.init_gevent() every Xray gRPC call blocks the gunicorn gevent hub. "
    "Keep the call at module scope in the role factory."
)


def _reimport_role(name):
    module_name = f"panel_core.roles.{name}"
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def _role_source(name):
    return source_path(f"roles/{name}.py")


@pytest.mark.parametrize("name", HEAVY_ROLES)
def test_heavy_roles_initialise_grpc_gevent(name):
    with patch.object(grpc_gevent, "init_gevent") as init_gevent:
        _reimport_role(name)

    assert init_gevent.call_count == 1, (
        f"panel_core.roles.{name} must call grpc_gevent.init_gevent() when the module is set up "
        f"(observed {init_gevent.call_count} calls). {INIT_GEVENT_HINT}"
    )


@pytest.mark.parametrize("name", LIGHT_ROLES)
def test_light_roles_do_not_initialise_grpc_gevent(name):
    with patch.object(grpc_gevent, "init_gevent") as init_gevent:
        _reimport_role(name)

    assert init_gevent.call_count == 0, (
        f"panel_core.roles.{name} is a light role and must not call grpc_gevent.init_gevent()"
    )


@pytest.mark.parametrize("name", LIGHT_ROLES)
def test_light_roles_do_not_import_grpc_under_any_spelling(name):
    offenders = sorted(mod for mod in imported_modules(_role_source(name)) if mod == "grpc" or mod.startswith("grpc."))
    assert offenders == [], (
        f"panel_core.roles.{name} imports {offenders} — a light role must not pull grpc in, "
        "regardless of how the import is spelled"
    )


APPLY_CONFIG_HINT = (
    "apply_config() is the one gateway call a light role is allowed to make: _sync_after_provision() "
    "calls generate_config_file() unconditionally, which on the bot role's gateway is a no-op that "
    "writes nothing and talks to no Xray. Every other method on _ExplodingGateway raises, so this count "
    "is the whole of the light role's Xray surface -- if it moved, either provisioning grew a second "
    "sync, or a real config write crept back onto a role that has no Xray to write for."
)


class _ExplodingGateway:
    def __init__(self):
        self.apply_config_calls = 0

    def has_local_xray(self):
        return False

    def apply_config(self, validate=True):
        self.apply_config_calls += 1
        return None

    def restart(self):
        raise AssertionError("xray restart reached in a light role")

    def add_user(self, inbound_tag, client_obj):
        raise AssertionError("xray gRPC add_user reached in a light role")

    def remove_user(self, inbound_tag, email):
        raise AssertionError("xray gRPC remove_user reached in a light role")

    def stream_logs(self, tail_lines=0):
        raise AssertionError("xray log tail reached in a light role")

    def update_geo(self):
        raise AssertionError("xray geo update reached in a light role")


@pytest.fixture
def bot_role_app(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "bot")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/grpc-isolation.db")
    monkeypatch.chdir(tmp_path)

    from panel_core.xray.gateway import set_xray_gateway

    gateway = _ExplodingGateway()
    set_xray_gateway(gateway)

    from panel_core.roles import botapi

    app = botapi.create_app()
    app.xray_gateway_spy = gateway
    assert gateway.apply_config_calls == 0, (
        f"botapi.create_app() wrote an Xray config while booting ({gateway.apply_config_calls} calls). "
        f"{APPLY_CONFIG_HINT}"
    )

    from panel_core.extensions import db
    from panel_core.models import SystemSetting

    with app.app_context():
        db.create_all()
        db.session.add(SystemSetting(key="bot_service_token", value="test-bot-token"))
        db.session.commit()

    yield app

    from panel_core.extensions import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    scheduler.remove_all_jobs()


@pytest.fixture
def bot_role_client(bot_role_app):
    return bot_role_app.test_client()


AUTH = {"Authorization": "Bearer test-bot-token"}


def _seed_trial_tariff(app, *, panel_id):
    from panel_core.extensions import db
    from panel_core.models import LinkedPanel, Tariff, TariffItem

    with app.app_context():
        if panel_id is not None:
            db.session.add(
                LinkedPanel(
                    id=panel_id,
                    name="node-1",
                    url="http://node-1:5000",
                    federation_token="tok",
                    created_at=0,
                )
            )
        tariff = Tariff(name="Trial", price_rub=0, period_days=3, is_trial=True, enabled=True)
        db.session.add(tariff)
        db.session.flush()
        db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="vless-reality", traffic_gb=10, panel_id=panel_id))
        db.session.commit()
        return tariff.id


def _local_client_count(app):
    from panel_core.models import Client

    with app.app_context():
        return Client.query.count()


def test_bot_role_trial_activate_provisions_on_a_node(bot_role_app, bot_role_client):
    _seed_trial_tariff(bot_role_app, panel_id=7)

    with patch("panel_core.services.panel_proxy.proxy_provision") as m_provision:
        resp = bot_role_client.post(
            "/api/bot-service/trial/activate",
            json={"telegram_id": 4242},
            headers=AUTH,
        )

    assert resp.status_code == 200
    assert m_provision.call_count == 1
    panel_id, telegram_id, inbound_tag, payload = m_provision.call_args.args
    assert (panel_id, telegram_id, inbound_tag) == (7, 4242, "vless-reality")
    assert payload["limit_bytes"] == 10 * 1024**3
    assert _local_client_count(bot_role_app) == 0
    assert bot_role_app.xray_gateway_spy.apply_config_calls == 1, (
        f"expected exactly one apply_config() from _sync_after_provision, observed "
        f"{bot_role_app.xray_gateway_spy.apply_config_calls}. {APPLY_CONFIG_HINT}"
    )


def test_bot_role_trial_activate_refuses_a_local_only_tariff(bot_role_app, bot_role_client):
    from panel_core.models import TelegramUser

    _seed_trial_tariff(bot_role_app, panel_id=None)

    resp = bot_role_client.post(
        "/api/bot-service/trial/activate",
        json={"telegram_id": 4242},
        headers=AUTH,
    )

    assert resp.status_code == 500
    assert _local_client_count(bot_role_app) == 0
    assert bot_role_app.xray_gateway_spy.apply_config_calls == 0, (
        f"a refused local-only tariff still reached apply_config "
        f"({bot_role_app.xray_gateway_spy.apply_config_calls} calls) — _require_local_xray() must abort "
        f"before any sync. {APPLY_CONFIG_HINT}"
    )
    with bot_role_app.app_context():
        from panel_core.extensions import db

        assert db.session.get(TelegramUser, 4242).trial_used_at is None


def test_bot_role_local_only_tariff_raises_local_xray_unavailable(bot_role_app):
    from panel_core.models import Tariff
    from panel_core.services.provisioning import apply_tariff_for_user
    from panel_core.xray.gateway import LocalXrayUnavailable

    tariff_id = _seed_trial_tariff(bot_role_app, panel_id=None)

    with bot_role_app.app_context():
        from panel_core.extensions import db

        tariff = db.session.get(Tariff, tariff_id)
        with pytest.raises(LocalXrayUnavailable) as excinfo:
            apply_tariff_for_user(4242, tariff, source="trial")

    assert "panel_id" in str(excinfo.value)


def test_bot_role_checkout_is_handled_locally(bot_role_client):
    tariff_payload = {"payment_id": 1, "confirmation_url": "https://yookassa.example/p/1"}

    with patch("panel_core.services.billing.create_checkout", return_value=tariff_payload) as m_checkout:
        resp = bot_role_client.post(
            "/api/billing/checkout",
            json={"telegram_id": 4242, "tariff_id": 1},
            headers=AUTH,
        )

    assert resp.status_code == 200
    assert resp.get_json() == tariff_payload
    assert m_checkout.call_count == 1
    assert m_checkout.call_args.kwargs == {"telegram_id": 4242, "tariff_id": 1, "lang": "ru"}


def test_bot_role_yookassa_webhook_is_served(bot_role_client):
    resp = bot_role_client.post(
        "/api/billing/yookassa/webhook",
        json={"event": "refund.succeeded", "object": {"payment_id": "yk-unknown"}},
    )

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
