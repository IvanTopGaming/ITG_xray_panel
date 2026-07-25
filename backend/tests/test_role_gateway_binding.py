import importlib

import pytest

from panel_core.xray import gateway as gw

CASES = [
    ("worker", "worker", gw.LocalXrayGateway),
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
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/{module_name}.db")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    module = importlib.import_module(f"panel_core.roles.{module_name}")
    module.create_app()

    assert isinstance(gw.get_xray_gateway(), expected)


@pytest.mark.parametrize("module_name,role,expected", CASES, ids=[c[0] for c in CASES])
def test_preconfigured_gateway_wins(module_name, role, expected, monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/{module_name}-preset.db")
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


def test_master_gateway_refuses_local_user_mutation(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/master-refuse.db")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    from panel_core.roles import master

    master.create_app()

    with pytest.raises(gw.LocalXrayUnavailable):
        gw.get_xray_gateway().add_user("tag", object())
