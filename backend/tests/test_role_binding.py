import importlib
import os

import pytest

ROLE_MODULES = [
    ("sub", "sub"),
    ("botapi", "bot"),
    ("worker", "worker"),
    ("master", "master"),
]

CONTRADICTIONS = {
    "sub": "master",
    "bot": "worker",
    "worker": "sub",
    "master": "bot",
}


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


def _create(module_name):
    _reset_scheduler()
    module = importlib.import_module(f"panel_core.roles.{module_name}")
    return module.create_app()


def _guards():
    from panel_core.panel_role import is_bot_api, is_sub, is_worker

    return {"sub": is_sub(), "bot": is_bot_api(), "worker": is_worker()}


def _expected_guards(role):
    return {"sub": role == "sub", "bot": role == "bot", "worker": role == "worker"}


@pytest.mark.parametrize("module_name,role", ROLE_MODULES, ids=[m for m, _ in ROLE_MODULES])
def test_factory_binds_its_own_role_when_env_is_unset(module_name, role, monkeypatch, tmp_path):
    monkeypatch.delenv("PANEL_ROLE", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/bind-{module_name}.db")
    monkeypatch.chdir(tmp_path)

    _create(module_name)

    assert os.environ["PANEL_ROLE"] == role
    assert _guards() == _expected_guards(role)


@pytest.mark.parametrize("module_name,role", ROLE_MODULES, ids=[m for m, _ in ROLE_MODULES])
def test_factory_refuses_to_boot_on_a_contradicting_env_role(module_name, role, monkeypatch, tmp_path):
    contradiction = CONTRADICTIONS[role]
    monkeypatch.setenv("PANEL_ROLE", contradiction)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/clash-{module_name}.db")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError) as excinfo:
        _create(module_name)

    message = str(excinfo.value)
    assert "PANEL_ROLE" in message
    assert contradiction in message
    assert role in message


@pytest.mark.parametrize("module_name,role", ROLE_MODULES, ids=[m for m, _ in ROLE_MODULES])
def test_factory_accepts_a_matching_env_role(module_name, role, monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", role.upper())
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/match-{module_name}.db")
    monkeypatch.chdir(tmp_path)

    _create(module_name)

    assert os.environ["PANEL_ROLE"] == role
    assert _guards() == _expected_guards(role)


def _bot_app(monkeypatch, tmp_path, db_name):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/{db_name}.db")
    monkeypatch.delenv("ADMIN_BACKEND_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    from panel_core.roles import botapi

    return botapi.create_app()


def test_bot_role_webhook_404s_with_panel_role_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("PANEL_ROLE", raising=False)

    app = _bot_app(monkeypatch, tmp_path, "bot-unset")
    resp = app.test_client().post(
        "/api/billing/yookassa/webhook",
        json={"event": "refund.succeeded", "object": {"payment_id": "yk-1"}},
    )

    assert resp.status_code == 404


def test_bot_role_webhook_is_never_served_with_a_contradicting_panel_role(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "master")

    with pytest.raises(RuntimeError):
        _bot_app(monkeypatch, tmp_path, "bot-clash")


def test_root_dispatcher_still_routes_by_env_role(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "sub")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/dispatch.db")
    monkeypatch.chdir(tmp_path)

    _reset_scheduler()

    import panel_core

    app = panel_core.create_app()

    assert set(app.blueprints) == {"subscription"}
    assert os.environ["PANEL_ROLE"] == "sub"
