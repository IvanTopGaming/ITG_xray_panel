import pytest


def _clear_jobs():
    from panel_core.extensions import scheduler

    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


def test_sub_role_registers_only_subscription(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "sub")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/sub.db")
    monkeypatch.chdir(tmp_path)
    _clear_jobs()

    from panel_core.roles import sub

    app = sub.create_app()
    assert set(app.blueprints) == {"subscription"}


def test_botapi_role_registers_bot_service_and_billing(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "bot")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/bot.db")
    monkeypatch.chdir(tmp_path)
    _clear_jobs()

    from panel_core.roles import botapi

    app = botapi.create_app()
    assert set(app.blueprints) == {"bot_service", "billing"}


@pytest.mark.parametrize("module_name", ["sub", "botapi"])
def test_light_roles_register_no_jobs(module_name, monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "sub" if module_name == "sub" else "bot")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/{module_name}-jobs.db")
    monkeypatch.chdir(tmp_path)
    _clear_jobs()

    import importlib

    module = importlib.import_module(f"panel_core.roles.{module_name}")
    module.create_app()

    from panel_core.extensions import scheduler

    assert scheduler.get_jobs() == []
