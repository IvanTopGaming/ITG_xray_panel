import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "packages" / "panel-core" / "src" / "panel_core"


def _imported_modules(path):
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
    return found


def test_services_do_not_import_api():
    offenders = []
    for path in (SRC / "services").rglob("*.py"):
        for mod in _imported_modules(path):
            if mod.startswith("panel_core.api"):
                offenders.append(f"{path.name} -> {mod}")
    assert offenders == []


def test_services_do_not_import_jobs():
    offenders = []
    for path in (SRC / "services").rglob("*.py"):
        for mod in _imported_modules(path):
            if mod.startswith("panel_core.jobs"):
                offenders.append(f"{path.name} -> {mod}")
    assert offenders == []


def test_api_modules_do_not_import_each_other():
    offenders = []
    for path in (SRC / "api").rglob("*.py"):
        for mod in _imported_modules(path):
            if mod.startswith("panel_core.api.") and not mod.endswith(path.stem):
                offenders.append(f"{path.name} -> {mod}")
    assert offenders == []


@pytest.mark.parametrize(
    "module,attr",
    [
        ("panel_core.services.remote_clients", "remote_clients_by_telegram_id_live"),
        ("panel_core.services.notifications", "emit_if_new"),
        ("panel_core.services.notifications", "evaluate_traffic"),
        ("panel_core.services.notifications", "evaluate_expiry"),
        ("panel_core.services.sub_links", "build_aggregate_sub_url"),
    ],
)
def test_extracted_helpers_available(module, attr):
    import importlib

    assert callable(getattr(importlib.import_module(module), attr))
