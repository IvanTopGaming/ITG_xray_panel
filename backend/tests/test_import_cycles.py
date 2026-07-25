import pytest

from tests.import_graph import SRC, imported_modules


def _package_sources(package):
    root = SRC / package
    paths = sorted(root.rglob("*.py"))
    assert paths, (
        f"no python sources found under {root} — this guard would pass vacuously. "
        f"If the '{package}' package moved, point SRC/the guard at its new location."
    )
    return paths


def _offenders(package, forbidden_prefix):
    offenders = set()
    for path in _package_sources(package):
        for mod in imported_modules(path):
            parts = mod.split(".")
            if parts[: len(forbidden_prefix)] == forbidden_prefix:
                offenders.add(f"{path.name} -> {'.'.join(parts[: len(forbidden_prefix) + 1])}")
    return sorted(offenders)


def test_services_do_not_import_api():
    assert _offenders("services", ["panel_core", "api"]) == []


def test_services_do_not_import_jobs():
    assert _offenders("services", ["panel_core", "jobs"]) == []


def test_api_modules_do_not_import_each_other():
    offenders = set()
    for path in _package_sources("api"):
        for mod in imported_modules(path):
            parts = mod.split(".")
            if len(parts) >= 3 and parts[:2] == ["panel_core", "api"] and parts[2] != path.stem:
                offenders.add(f"{path.name} -> {'.'.join(parts[:3])}")
    assert sorted(offenders) == []


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
