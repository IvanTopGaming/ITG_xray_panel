from unittest.mock import MagicMock

import pytest

from panel_core.xray import gateway as gw


@pytest.fixture(autouse=True)
def _reset_gateway():
    yield
    gw.set_xray_gateway(None)


def test_default_gateway_is_local():
    gw.set_xray_gateway(None)
    assert isinstance(gw.get_xray_gateway(), gw.LocalXrayGateway)


def test_set_gateway_is_returned():
    fake = MagicMock()
    gw.set_xray_gateway(fake)
    assert gw.get_xray_gateway() is fake


def test_facade_delegates_apply_config():
    import panel_core.xray as facade

    fake = MagicMock()
    gw.set_xray_gateway(fake)
    facade.generate_config_file()
    fake.apply_config.assert_called_once_with(validate=True)


def test_facade_delegates_apply_config_without_validation():
    import panel_core.xray as facade

    fake = MagicMock()
    gw.set_xray_gateway(fake)
    facade.generate_config_file(validate=False)
    fake.apply_config.assert_called_once_with(validate=False)


def test_facade_delegates_restart():
    import panel_core.xray as facade

    fake = MagicMock()
    gw.set_xray_gateway(fake)
    facade.restart_xray_container()
    fake.restart.assert_called_once_with()


def test_facade_delegates_add_and_remove_user():
    import panel_core.xray as facade

    fake = MagicMock()
    fake.add_user.return_value = True
    fake.remove_user.return_value = True
    gw.set_xray_gateway(fake)

    assert facade._api_add_user_grpc("in-tag", "client") is True
    fake.add_user.assert_called_once_with("in-tag", "client")

    assert facade._api_remove_user_grpc("in-tag", "a@b") is True
    fake.remove_user.assert_called_once_with("in-tag", "a@b")


def test_facade_delegates_stream_logs_and_update_geo():
    import panel_core.xray as facade

    fake = MagicMock()
    gw.set_xray_gateway(fake)

    facade.stream_xray_logs(50)
    fake.stream_logs.assert_called_once_with(50)

    facade.update_geo_db()
    fake.update_geo.assert_called_once_with()


def test_facade_stream_logs_without_arguments_uses_engine_default():
    import panel_core.xray as facade
    from panel_core.xray import engine

    fake = MagicMock()
    gw.set_xray_gateway(fake)

    facade.stream_xray_logs()

    fake.stream_logs.assert_called_once_with(engine.LOG_TAIL_LINES)


def test_local_gateway_stream_logs_without_arguments_uses_engine_default(monkeypatch):
    from panel_core.xray import engine

    seen = {}
    monkeypatch.setattr(engine, "stream_xray_logs", lambda tail_lines: seen.setdefault("tail", tail_lines))

    gw.LocalXrayGateway().stream_logs()

    assert seen == {"tail": engine.LOG_TAIL_LINES}


@pytest.mark.parametrize(
    "facade_name,gateway_name,args",
    [
        ("generate_config_file", "apply_config", ()),
        ("restart_xray_container", "restart", ()),
        ("stream_xray_logs", "stream_logs", ()),
        ("update_geo_db", "update_geo", ()),
        ("_api_add_user_grpc", "add_user", ("tag", "client")),
        ("_api_remove_user_grpc", "remove_user", ("tag", "a@b")),
    ],
)
def test_facade_propagates_gateway_return_value(facade_name, gateway_name, args):
    import panel_core.xray as facade

    sentinel = object()
    fake = MagicMock()
    getattr(fake, gateway_name).return_value = sentinel
    gw.set_xray_gateway(fake)

    assert getattr(facade, facade_name)(*args) is sentinel


@pytest.mark.parametrize(
    "facade_name,impl_module,impl_name",
    [
        ("generate_config_file", "panel_core.xray.engine", "generate_config_file"),
        ("restart_xray_container", "panel_core.xray.engine", "restart_xray_container"),
        ("stream_xray_logs", "panel_core.xray.engine", "stream_xray_logs"),
        ("update_geo_db", "panel_core.xray.engine", "update_geo_db"),
        ("_api_add_user_grpc", "panel_core.xray.grpc_client", "_api_add_user_grpc"),
        ("_api_remove_user_grpc", "panel_core.xray.grpc_client", "_api_remove_user_grpc"),
    ],
)
def test_facade_signature_matches_implementation(facade_name, impl_module, impl_name):
    import importlib
    import inspect

    import panel_core.xray as facade

    def shape(fn):
        return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]

    impl = getattr(importlib.import_module(impl_module), impl_name)

    assert shape(getattr(facade, facade_name)) == shape(impl)


def test_local_gateway_satisfies_the_protocol():
    assert isinstance(gw.LocalXrayGateway(), gw.XrayGateway)


def test_local_gateway_delegates_to_engine(monkeypatch):
    from panel_core.xray import engine

    calls = {}
    monkeypatch.setattr(engine, "generate_config_file", lambda validate=True: calls.setdefault("apply", validate))
    monkeypatch.setattr(engine, "restart_xray_container", lambda: calls.setdefault("restart", True))

    local = gw.LocalXrayGateway()
    local.apply_config(validate=False)
    local.restart()

    assert calls == {"apply": False, "restart": True}


def test_local_gateway_delegates_to_grpc_client(monkeypatch):
    from panel_core.xray import grpc_client

    calls = {}

    def _add(tag, client_obj):
        calls["add"] = (tag, client_obj)
        return True

    def _remove(tag, email):
        calls["remove"] = (tag, email)
        return True

    monkeypatch.setattr(grpc_client, "_api_add_user_grpc", _add)
    monkeypatch.setattr(grpc_client, "_api_remove_user_grpc", _remove)

    local = gw.LocalXrayGateway()
    assert local.add_user("tag-a", "obj") is True
    assert local.remove_user("tag-a", "a@b") is True

    assert calls == {"add": ("tag-a", "obj"), "remove": ("tag-a", "a@b")}
