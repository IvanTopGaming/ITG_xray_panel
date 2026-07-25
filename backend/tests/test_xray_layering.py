import importlib
import json
import subprocess
import sys
import textwrap

import pytest

from tests.import_graph import SRC, import_chains, imported_modules

HEAVY_RUNTIME_MODULES = (
    "docker",
    "grpc",
    "filelock",
    "app.proxyman",
    "app.stats",
    "common",
    "proxy",
)

_SEAM_IMPORT_PROBE = """
import importlib
import json
import sys
import types

pkg = types.ModuleType("panel_core")
pkg.__path__ = [{src!r}]
sys.modules["panel_core"] = pkg

importlib.import_module("panel_core.xray")
importlib.import_module("panel_core.xray.gateway")

print(json.dumps(sorted(m for m in {forbidden!r} if m in sys.modules)))
"""

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

HEAVY_MODULES = ("panel_core.xray.engine", "panel_core.xray.grpc_client") + HEAVY_RUNTIME_MODULES

ALLOWED_HEAVY_IMPORTERS = {"gateway.py", "engine.py", "grpc_client.py"}

ALLOWED_HEAVY_ROLE_IMPORTERS = {"worker.py", "master.py"}

ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE = {
    "xray": ALLOWED_HEAVY_IMPORTERS,
    "roles": ALLOWED_HEAVY_ROLE_IMPORTERS,
}

KNOWN_HEAVY_IMPORT_VIOLATIONS = [
    "api/inbound.py -> panel_core.services.stats -> panel_core.xray.engine",
    "api/inbound.py -> panel_core.services.stats -> panel_core.xray.grpc_client",
]


def _heavy_root(module):
    for heavy in HEAVY_MODULES:
        if module == heavy or module.startswith(f"{heavy}."):
            return heavy
    return None


def _heavy_targets(path):
    return sorted({root for mod in imported_modules(path) if (root := _heavy_root(mod))})


def _package_sources(package):
    root = SRC / package
    paths = sorted(root.rglob("*.py"))
    assert paths, (
        f"no python sources found under {root} — this guard would pass vacuously. "
        f"If the '{package}' package moved, point SRC/the guard at its new location."
    )
    return paths


def _heavy_offenders(package):
    allowed = ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE.get(package, set())
    offenders = []
    for path in _package_sources(package):
        if path.name in allowed:
            continue
        for heavy in _heavy_targets(path):
            offenders.append(f"{package}/{path.name} -> {heavy}")
    return offenders


def _transitive_heavy_offenders(package):
    allowed = ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE.get(package, set())
    offenders = set()
    for path in _package_sources(package):
        if path.name in allowed:
            continue
        for module, chain in import_chains(path).items():
            root = _heavy_root(module)
            if root is None:
                continue
            hops = chain[1:-1]
            if any(_heavy_root(hop) for hop in hops):
                continue
            offenders.add(" -> ".join([f"{package}/{path.name}"] + hops + [root]))
    return sorted(offenders)


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
    assert _heavy_offenders("api") == []


def test_only_gateway_imports_heavy_xray_modules_inside_xray_package():
    assert _heavy_offenders("xray") == []


def test_only_worker_and_master_roles_import_heavy_modules():
    assert _heavy_offenders("roles") == []


def test_api_layer_does_not_transitively_reach_heavy_modules():
    assert _transitive_heavy_offenders("api") == KNOWN_HEAVY_IMPORT_VIOLATIONS


def test_xray_package_does_not_transitively_reach_heavy_modules():
    assert _transitive_heavy_offenders("xray") == []


def test_light_roles_do_not_transitively_reach_heavy_modules():
    assert _transitive_heavy_offenders("roles") == []


def test_xray_seam_imports_without_heavy_dependencies():
    probe = textwrap.dedent(_SEAM_IMPORT_PROBE).format(src=str(SRC), forbidden=list(HEAVY_RUNTIME_MODULES))
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"importing the xray seam failed:\n{result.stderr}"
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


def test_protocol_and_settings_are_free_of_heavy_imports():
    for name in ("protocol.py", "settings.py"):
        source = (SRC / "xray" / name).read_text()
        for forbidden in ("import docker", "import grpc", "from filelock", "import subprocess"):
            assert forbidden not in source, f"{name} contains {forbidden}"
