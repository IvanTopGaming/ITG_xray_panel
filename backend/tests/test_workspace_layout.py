import importlib
import importlib.util

import pytest


def test_panel_core_is_importable():
    mod = importlib.import_module("panel_core")
    assert hasattr(mod, "create_app")


@pytest.mark.parametrize(
    "name",
    [
        "panel_core.models",
        "panel_core.extensions",
        "panel_core.db_config",
        "panel_core.panel_role",
        "panel_core.pg_migrate",
        "panel_core.db_migration",
        "panel_core.utils",
        "panel_core.version",
        "panel_core.observability",
        "panel_core.pg_compat",
        "panel_core.api.auth",
        "panel_core.api.inbound",
        "panel_core.api.subscription",
        "panel_core.jobs.payments",
        "panel_core.services.provisioning",
        "panel_core.services.panel_proxy",
    ],
)
def test_submodules_importable(name):
    assert importlib.import_module(name) is not None


def test_application_no_longer_lives_under_app_namespace():
    assert importlib.util.find_spec("panel_core.models") is not None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.models")
