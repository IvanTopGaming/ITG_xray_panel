import importlib
import importlib.util

import pytest

from tests.import_graph import SRC, imported_modules

ENGINE_EXPORTS = [
    "generate_config_file",
    "restart_xray_container",
    "stream_xray_logs",
    "update_geo_db",
]

ENGINE_CONSTANTS = [
    "CONFIG_PATH",
    "LOCK_PATH",
    "CANDIDATE_PATH",
    "XRAY_CONTAINER_NAME",
    "XRAY_BIN",
    "ACCESS_LOG_PATH",
    "ERROR_LOG_PATH",
    "LOG_TAIL_LINES",
]

HEAVY_MODULES = ("panel_core.xray.engine", "panel_core.xray.grpc_client")

ALLOWED_HEAVY_IMPORTERS = {"gateway.py", "engine.py", "grpc_client.py"}

KNOWN_HEAVY_IMPORT_VIOLATIONS = [
    "api/inbound.py -> panel_core.xray.engine",
    "xray/__init__.py -> panel_core.xray.engine",
]


def _heavy_targets(path):
    targets = set()
    for mod in imported_modules(path):
        for heavy in HEAVY_MODULES:
            if mod == heavy or mod.startswith(f"{heavy}."):
                targets.add(heavy)
    return sorted(targets)


def _heavy_offenders(package):
    offenders = []
    for path in sorted((SRC / package).rglob("*.py")):
        if package == "xray" and path.name in ALLOWED_HEAVY_IMPORTERS:
            continue
        for heavy in _heavy_targets(path):
            offenders.append(f"{package}/{path.name} -> {heavy}")
    return offenders


@pytest.mark.parametrize("name", ENGINE_EXPORTS)
def test_engine_exposes_runtime_function(name):
    mod = importlib.import_module("panel_core.xray.engine")
    assert callable(getattr(mod, name))


@pytest.mark.parametrize("name", ENGINE_CONSTANTS)
def test_engine_exposes_constant(name):
    mod = importlib.import_module("panel_core.xray.engine")
    assert getattr(mod, name) is not None


def test_old_services_xray_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("panel_core.services.xray")


def test_api_layer_does_not_import_heavy_xray_modules():
    expected = [v for v in KNOWN_HEAVY_IMPORT_VIOLATIONS if v.startswith("api/")]
    assert _heavy_offenders("api") == expected


def test_only_gateway_imports_heavy_xray_modules_inside_xray_package():
    expected = [v for v in KNOWN_HEAVY_IMPORT_VIOLATIONS if v.startswith("xray/")]
    assert _heavy_offenders("xray") == expected


def test_known_heavy_import_violations_do_not_grow():
    assert _heavy_offenders("api") + _heavy_offenders("xray") == KNOWN_HEAVY_IMPORT_VIOLATIONS


def test_protocol_and_settings_are_free_of_heavy_imports():
    for name in ("protocol.py", "settings.py"):
        source = (SRC / "xray" / name).read_text()
        for forbidden in ("import docker", "import grpc", "from filelock", "import subprocess"):
            assert forbidden not in source, f"{name} contains {forbidden}"
