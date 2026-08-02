from unittest.mock import MagicMock

import pytest

from panel_core.xray import gateway as gw
from panel_core.xray.local import LocalXrayGateway


@pytest.fixture(autouse=True)
def _reset_gateway():
    yield
    gw.set_xray_gateway(None)


def test_unbound_gateway_fails_loud_instead_of_defaulting_to_local():
    gw.set_xray_gateway(None)
    gw.set_default_xray_gateway(None)
    with pytest.raises(RuntimeError) as excinfo:
        gw.get_xray_gateway()
    assert "no XrayGateway bound" in str(excinfo.value)


def test_local_gateway_ships_from_its_own_module():
    from panel_core.xray import local

    assert not hasattr(gw, "LocalXrayGateway"), (
        "LocalXrayGateway must not be reachable from panel_core.xray.gateway. gateway.py ships from "
        "panel-core; LocalXrayGateway ships from panel-worker, and a re-export here would make core "
        "import from a downstream distribution."
    )
    assert local.LocalXrayGateway().has_local_xray() is True


def test_set_gateway_is_returned():
    fake = MagicMock()
    gw.set_xray_gateway(fake)
    assert gw.get_xray_gateway() is fake


def test_facade_delegates_apply_config():
    from panel_core.xray import facade

    fake = MagicMock()
    gw.set_xray_gateway(fake)
    facade.generate_config_file()
    fake.apply_config.assert_called_once_with(validate=True)


def test_facade_delegates_apply_config_without_validation():
    from panel_core.xray import facade

    fake = MagicMock()
    gw.set_xray_gateway(fake)
    facade.generate_config_file(validate=False)
    fake.apply_config.assert_called_once_with(validate=False)


def test_facade_delegates_restart():
    from panel_core.xray import facade

    fake = MagicMock()
    gw.set_xray_gateway(fake)
    facade.restart_xray_container()
    fake.restart.assert_called_once_with()


def test_facade_delegates_add_and_remove_user():
    from panel_core.xray import facade

    fake = MagicMock()
    fake.add_user.return_value = True
    fake.remove_user.return_value = True
    gw.set_xray_gateway(fake)

    assert facade._api_add_user_grpc("in-tag", "client") is True
    fake.add_user.assert_called_once_with("in-tag", "client")

    assert facade._api_remove_user_grpc("in-tag", "a@b") is True
    fake.remove_user.assert_called_once_with("in-tag", "a@b")


def test_facade_delegates_stream_logs_and_update_geo():
    from panel_core.xray import facade

    fake = MagicMock()
    gw.set_xray_gateway(fake)

    facade.stream_xray_logs(50)
    fake.stream_logs.assert_called_once_with(50)

    facade.update_geo_db()
    fake.update_geo.assert_called_once_with()


def test_facade_stream_logs_without_arguments_uses_engine_default():
    from panel_core.xray import facade
    from panel_core.xray import engine

    fake = MagicMock()
    gw.set_xray_gateway(fake)

    facade.stream_xray_logs()

    fake.stream_logs.assert_called_once_with(engine.LOG_TAIL_LINES)


def test_local_gateway_stream_logs_without_arguments_uses_engine_default(monkeypatch):
    from panel_core.xray import engine

    seen = {}
    monkeypatch.setattr(engine, "stream_xray_logs", lambda tail_lines: seen.setdefault("tail", tail_lines))

    LocalXrayGateway().stream_logs()

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
    from panel_core.xray import facade

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

    from panel_core.xray import facade

    def shape(fn):
        return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]

    impl = getattr(importlib.import_module(impl_module), impl_name)

    assert shape(getattr(facade, facade_name)) == shape(impl)


def test_local_gateway_satisfies_the_protocol():
    assert isinstance(LocalXrayGateway(), gw.XrayGateway)


def test_local_gateway_delegates_to_engine(monkeypatch):
    from panel_core.xray import engine

    calls = {}
    monkeypatch.setattr(engine, "generate_config_file", lambda validate=True: calls.setdefault("apply", validate))
    monkeypatch.setattr(engine, "restart_xray_container", lambda: calls.setdefault("restart", True))

    local = LocalXrayGateway()
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

    local = LocalXrayGateway()
    assert local.add_user("tag-a", "obj") is True
    assert local.remove_user("tag-a", "a@b") is True

    assert calls == {"add": ("tag-a", "obj"), "remove": ("tag-a", "a@b")}


def test_gateway_configured_predicate():
    gw.set_xray_gateway(None)
    assert gw.xray_gateway_configured() is False
    gw.set_xray_gateway(MagicMock())
    assert gw.xray_gateway_configured() is True


def _build_app():
    from panel_core.dispatch import create_app
    from panel_core.extensions import scheduler

    try:
        create_app()
    finally:
        scheduler.remove_all_jobs()
        if scheduler.running:
            scheduler.shutdown(wait=False)


def test_create_app_does_not_clobber_an_installed_gateway():
    fake = MagicMock()
    gw.set_xray_gateway(fake)
    _build_app()
    assert gw.get_xray_gateway() is fake


def test_create_app_installs_remote_gateway_for_the_default_master_role():
    gw.set_xray_gateway(None)
    _build_app()
    assert isinstance(gw.get_xray_gateway(), gw.RemoteXrayGateway)


def test_the_default_gateway_hook_is_test_only():
    from tests.import_graph import TOP_LEVEL_ENTRY_POINTS, iter_sources, relative_source_name, source_path

    assert "def set_default_xray_gateway" in source_path("xray/gateway.py").read_text(), (
        "panel_core.xray.gateway no longer defines set_default_xray_gateway. This guard matches the hook "
        "by name, so renaming it makes every assertion below scan for a string that occurs nowhere and "
        "pass while a production caller of the new name goes unnoticed."
    )

    scanned = sorted(relative_source_name(path) for path in iter_sources())
    assert len(scanned) > 1, f"only {scanned} scanned - iter_sources() is broken and this guard is vacuous"

    offenders = sorted(
        relative_source_name(path)
        for path in iter_sources()
        if "set_default_xray_gateway" in path.read_text() and relative_source_name(path) != "xray/gateway.py"
    )

    for entry_point in TOP_LEVEL_ENTRY_POINTS:
        assert entry_point.is_file(), f"expected top-level entry point at {entry_point}"
        if "set_default_xray_gateway" in entry_point.read_text():
            offenders.append(entry_point.name)
    offenders.sort()

    assert offenders == [], (
        f"set_default_xray_gateway is a test-only fallback hook, called by tests/conftest.py alone: "
        f"{offenders}. Production must reach get_xray_gateway() with an explicitly bound gateway or fail "
        "loud; a caller inside packages/ or the top-level entry-point scripts (run.py, migrate_db.py, "
        "sqlite_to_pg.py) would silently restore the implicit-local default that this phase removed. "
        "This is a string match on the call site — a direct gateway._default_gateway assignment would "
        "evade it, but that is a different threat model and out of scope for this guard."
    )
