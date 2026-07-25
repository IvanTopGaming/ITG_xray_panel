import importlib

import pytest

from tests.call_graph import call_graph

GRPC_SINKS = {
    "panel_core.xray.grpc_client:get_channel",
    "panel_core.xray.grpc_client:_api_add_user_grpc",
    "panel_core.xray.grpc_client:_api_remove_user_grpc",
}

SUB_ROLE_MODULES = ("panel_core.api.subscription",)
BOT_ROLE_MODULES = ("panel_core.api.bot_service", "panel_core.api.billing")

ROLE_GUARDED_HANDLERS = {
    "panel_core.api.bot_service:activate_trial",
    "panel_core.api.billing:yookassa_webhook",
}

FAILURE_HINT = (
    "A light-role endpoint gained a call path to Xray gRPC. Either gate it behind is_bot_api()/is_sub() "
    "the way activate_trial and yookassa_webhook are, or make that role call grpc_gevent.init_gevent() "
    "in its factory before the call can block the gevent hub."
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


def test_only_role_guarded_bot_endpoints_can_reach_xray_grpc():
    reaching = _reaching(*BOT_ROLE_MODULES)
    assert set(reaching) == ROLE_GUARDED_HANDLERS, FAILURE_HINT + f"\nreaching: {reaching}"


def test_heavy_roles_initialise_grpc_gevent():
    for name in ("worker", "master"):
        module = importlib.import_module(f"panel_core.roles.{name}")
        assert hasattr(module, "grpc_gevent")


def test_light_roles_do_not_initialise_grpc_gevent():
    for name in ("sub", "botapi"):
        module = importlib.import_module(f"panel_core.roles.{name}")
        assert not hasattr(module, "grpc_gevent")


class _ExplodingGateway:
    def apply_config(self, validate=True):
        raise AssertionError("xray config write reached in a light role")

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
def bot_role_client(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "bot")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/grpc-isolation.db")
    monkeypatch.delenv("ADMIN_BACKEND_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    from panel_core.xray.gateway import set_xray_gateway

    set_xray_gateway(_ExplodingGateway())

    from panel_core.roles import botapi

    app = botapi.create_app()

    from panel_core.extensions import db
    from panel_core.models import SystemSetting

    with app.app_context():
        db.create_all()
        db.session.add(SystemSetting(key="bot_service_token", value="test-bot-token"))
        db.session.commit()

    return app.test_client()


def test_bot_role_trial_activate_never_provisions_locally(bot_role_client):
    resp = bot_role_client.post(
        "/api/bot-service/trial/activate",
        json={"telegram_id": 4242},
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert resp.status_code == 503
    assert resp.get_json() == {"error": "provisioning temporarily unavailable"}


def test_bot_role_checkout_never_provisions_locally(bot_role_client):
    resp = bot_role_client.post(
        "/api/billing/checkout",
        json={"telegram_id": 4242, "tariff_id": 1},
        headers={"Authorization": "Bearer test-bot-token"},
    )
    assert resp.status_code == 503
    assert resp.get_json() == {"error": "provisioning temporarily unavailable"}


def test_bot_role_yookassa_webhook_is_disabled(bot_role_client):
    resp = bot_role_client.post(
        "/api/billing/yookassa/webhook",
        json={"event": "refund.succeeded", "object": {"payment_id": "yk-1"}},
    )
    assert resp.status_code == 404
