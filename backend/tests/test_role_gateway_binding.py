import importlib

import pytest

from panel_core.xray import gateway as gw
from panel_core.xray.local import LocalXrayGateway

from tests.schema import ensure_schema

CASES = [
    ("worker", "worker", LocalXrayGateway),
    ("master", "master", gw.RemoteXrayGateway),
    ("sub", "sub", gw.NullXrayGateway),
    ("botapi", "bot", gw.NullXrayGateway),
]


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


@pytest.mark.parametrize("module_name,role,expected", CASES, ids=[c[0] for c in CASES])
def test_role_installs_its_gateway(module_name, role, expected, monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/{module_name}.db"))
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    module = importlib.import_module(f"panel_core.roles.{module_name}")
    module.create_app()

    assert isinstance(gw.get_xray_gateway(), expected)


@pytest.mark.parametrize("module_name,role,expected", CASES, ids=[c[0] for c in CASES])
def test_preconfigured_gateway_wins(module_name, role, expected, monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/{module_name}-preset.db"))
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()

    preset = _Sentinel()
    gw.set_xray_gateway(preset)

    module = importlib.import_module(f"panel_core.roles.{module_name}")
    module.create_app()

    assert gw.get_xray_gateway() is preset


class _Sentinel:
    def apply_config(self, validate=True):
        return None

    def restart(self):
        return None

    def add_user(self, inbound_tag, client_obj):
        return True

    def remove_user(self, inbound_tag, email):
        return True

    def stream_logs(self, tail_lines=0):
        return iter(())

    def update_geo(self):
        return None


@pytest.mark.parametrize("module_name,role,expected", CASES, ids=[c[0] for c in CASES])
def test_lazy_default_does_not_latch_and_preempt_the_role(module_name, role, expected, monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/{module_name}-lazy.db"))
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)
    gw.set_default_xray_gateway(None)

    with pytest.raises(RuntimeError) as excinfo:
        gw.get_xray_gateway()
    assert "no XrayGateway bound" in str(excinfo.value)
    assert gw.xray_gateway_configured() is False

    module = importlib.import_module(f"panel_core.roles.{module_name}")
    module.create_app()

    assert isinstance(gw.get_xray_gateway(), expected)


@pytest.mark.parametrize("module_name,role,expected", CASES, ids=[c[0] for c in CASES])
def test_role_installs_its_gateway_under_the_bare_autouse_fixture(module_name, role, expected, monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/{module_name}-bare.db"))
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()

    module = importlib.import_module(f"panel_core.roles.{module_name}")
    module.create_app()

    assert isinstance(gw.get_xray_gateway(), expected), (
        "create_app() must install the role's own gateway under nothing but the autouse "
        "_reset_xray_gateway fixture -- no test-local set_xray_gateway(None) call. A master here "
        "must come back RemoteXrayGateway (has_local_xray() False), not LocalXrayGateway/True; the "
        "reviewer measured exactly that inversion when the fixture pre-bound instead of only setting "
        "the fallback."
    )


def test_the_autouse_fixture_does_not_preempt_the_role_binding():
    gw.get_xray_gateway()

    assert gw.xray_gateway_configured() is False, (
        "the autouse _reset_xray_gateway fixture must leave the binding slot EMPTY so every role "
        "factory's `if not xray_gateway_configured()` still fires. Binding a gateway there instead of "
        "setting the fallback makes create_app() decline to bind, so a master app in tests silently "
        "gets a LocalXrayGateway and has_local_xray() answers True - inverting the Phase 3b invariant "
        "that the master has no local Xray."
    )


def test_master_gateway_refuses_local_user_mutation(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/master-refuse.db"))
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    from panel_core.roles import master

    master.create_app()

    with pytest.raises(gw.LocalXrayUnavailable):
        gw.get_xray_gateway().add_user("tag", object())
