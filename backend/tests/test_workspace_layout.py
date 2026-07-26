import importlib
import importlib.util

import pytest


def test_panel_core_is_importable():
    mod = importlib.import_module("panel_core")
    assert hasattr(mod, "__path__")
    assert callable(importlib.import_module("panel_core.dispatch").create_app)


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


def test_panel_sub_is_a_workspace_distribution():
    from tests.import_graph import SRC_ROOTS

    roots = {path.parents[1].name for path in SRC_ROOTS}
    assert "panel-sub" in roots, f"panel-sub must be a distribution under packages/; found {sorted(roots)}"


def test_subscription_modules_live_in_panel_sub():
    from tests.import_graph import source_path

    for relative in ("api/subscription.py", "roles/sub.py"):
        path = source_path(relative)
        assert "panel-sub" in path.parts, f"{relative} resolved to {path}, expected it under packages/panel-sub"


def test_subscription_still_imports_under_its_original_name():
    import panel_core.api.subscription
    import panel_core.roles.sub

    assert panel_core.api.subscription.bp.name == "subscription"
    assert callable(panel_core.roles.sub.create_app)


BOTAPI_SOURCES = (
    "api/bot_service.py",
    "api/billing.py",
    "services/billing.py",
    "jobs/payments.py",
    "roles/botapi.py",
)


def test_panel_botapi_is_a_workspace_distribution():
    from tests.import_graph import SRC_ROOTS

    roots = {path.parents[1].name for path in SRC_ROOTS}
    assert "panel-botapi" in roots, f"panel-botapi must be a distribution under packages/; found {sorted(roots)}"


def test_botapi_modules_live_in_panel_botapi():
    from tests.import_graph import source_path

    for relative in BOTAPI_SOURCES:
        path = source_path(relative)
        assert "panel-botapi" in path.parts, f"{relative} resolved to {path}, expected it under packages/panel-botapi"


def test_the_yookassa_sdk_is_imported_only_from_panel_botapi():
    import ast

    from tests.import_graph import iter_sources, root_for, root_label

    offenders = []
    for path in iter_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name == "yookassa" or name.startswith("yookassa.") for name in names):
                label = root_label(root_for(path))
                if not label.startswith("panel-botapi"):
                    offenders.append(f"{label}/{path.name}")
    assert offenders == [], (
        "the yookassa SDK must be imported only from panel-botapi, otherwise the dependency "
        f"leaks back into every image: {offenders}"
    )
