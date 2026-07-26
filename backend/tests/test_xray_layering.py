import importlib
import json
import subprocess
import sys
import textwrap

import pytest

from tests.import_graph import (
    HEAVY_ROOTS,
    HEAVY_ROOTS_DOC,
    SRC,
    XRAY_HEAVY_MODULES,
    heavy_root,
    import_chains,
    imported_modules,
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

roots = {roots!r}
print(json.dumps(sorted({{name.split(".")[0] for name in sys.modules if name.split(".")[0] in roots}})))
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

ALLOWED_HEAVY_IMPORTERS = {"gateway.py", "engine.py", "grpc_client.py"}

ALLOWED_HEAVY_ROLE_IMPORTERS = {"worker.py"}

ALLOWED_HEAVY_SERVICE_IMPORTERS = {"stats.py"}

ROOT_MODULES = "."

ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE = {
    "xray": ALLOWED_HEAVY_IMPORTERS,
    "roles": ALLOWED_HEAVY_ROLE_IMPORTERS,
    "services": ALLOWED_HEAVY_SERVICE_IMPORTERS,
    "jobs": set(),
    "api": set(),
    "data": set(),
    ROOT_MODULES: set(),
}

OFFENDER_HINT = (
    "A module outside the allowlist reached the heavy stack. Only the worker may. "
    f"{HEAVY_ROOTS_DOC} If a file legitimately belongs on the worker side, add it to "
    "ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE deliberately -- never widen the heavy definition."
)


def _heavy_root(module):
    return heavy_root(module, extra=XRAY_HEAVY_MODULES)


def _heavy_targets(path):
    return sorted({root for mod in imported_modules(path) if (root := _heavy_root(mod))})


def _package_sources(package):
    if package == ROOT_MODULES:
        root, paths = SRC, sorted(SRC.glob("*.py"))
    else:
        root = SRC / package
        paths = sorted(root.rglob("*.py"))
    assert paths, (
        f"no python sources found under {root} — this guard would pass vacuously. "
        f"If the '{package}' package moved, point SRC/the guard at its new location."
    )
    return paths


def _discovered_packages():
    names = {ROOT_MODULES}
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC)
        if len(relative.parts) > 1:
            names.add(relative.parts[0])
    return sorted(names)


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


@pytest.mark.parametrize("package", sorted(ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE))
def test_package_does_not_import_heavy_modules(package):
    offenders = _heavy_offenders(package)
    assert offenders == [], f"{offenders}\n\n{OFFENDER_HINT}"


@pytest.mark.parametrize("package", sorted(ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE))
def test_package_does_not_transitively_reach_heavy_modules(package):
    offenders = _transitive_heavy_offenders(package)
    assert offenders == [], f"{offenders}\n\n{OFFENDER_HINT}"


def test_every_panel_core_package_is_covered_by_the_layering_guard():
    discovered = _discovered_packages()
    assert len(discovered) > 1, (
        f"only {discovered} found under {SRC} — package discovery is broken and every coverage claim below "
        "is vacuous. Discovery must not depend on __init__.py: panel_core's api/ and services/ are "
        "namespace packages and have none."
    )
    uncovered = [name for name in discovered if name not in ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE]
    assert uncovered == [], (
        f"panel_core packages with no layering guard: {uncovered}. Every package must have an entry in "
        "ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE (an empty set means 'nothing here may be heavy'), otherwise a "
        f"new package silently escapes the check the way 'services' did. {HEAVY_ROOTS_DOC}"
    )


def test_xray_seam_imports_without_heavy_dependencies():
    probe = textwrap.dedent(_SEAM_IMPORT_PROBE).format(src=str(SRC), roots=list(HEAVY_ROOTS))
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"importing the xray seam failed:\n{result.stderr}"
    leaked = json.loads(result.stdout.strip().splitlines()[-1])
    assert leaked == [], f"importing the xray seam pulled heavy roots into sys.modules: {leaked}\n\n{HEAVY_ROOTS_DOC}"


def test_protocol_and_settings_are_free_of_heavy_imports():
    for name in ("protocol.py", "settings.py"):
        path = SRC / "xray" / name
        offenders = sorted({mod for mod in imported_modules(path) if heavy_root(mod, extra=("subprocess",))})
        assert offenders == [], (
            f"xray/{name} must stay importable anywhere — it is the pure part of the seam — but it imports "
            f"{offenders}. subprocess counts as heavy here: shelling out to the xray binary belongs in "
            f"engine.py. {HEAVY_ROOTS_DOC}"
        )
