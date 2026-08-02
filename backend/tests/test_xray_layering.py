import importlib
import json
import subprocess
import sys
import textwrap

import pytest

from tests.import_graph import (
    HEAVY_ROOTS,
    HEAVY_ROOTS_DOC,
    SRC_ROOTS,
    SRC_ROOTS_DOC,
    XRAY_HEAVY_MODULES,
    discovered_packages,
    heavy_root,
    import_chains,
    imported_modules,
    iter_root_modules,
    iter_sources,
)

_SEAM_IMPORT_PROBE = """
import importlib
import json
import sys
import types

pkg = types.ModuleType("panel_core")
pkg.__path__ = list({roots_path!r})
sys.modules["panel_core"] = pkg

importlib.import_module("panel_core.xray")
importlib.import_module("panel_core.xray.gateway")
importlib.import_module("panel_core.xray.local")

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

ALLOWED_HEAVY_IMPORTERS = {"local.py", "engine.py", "grpc_client.py"}

ALLOWED_HEAVY_ROLE_IMPORTERS = {"worker.py"}

ALLOWED_HEAVY_SERVICE_IMPORTERS = {"stats.py"}

ROOT_MODULES = "."

NAMESPACE_PACKAGES = {"api", "services", "jobs", "roles", "xray"}

ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE = {
    "xray": ALLOWED_HEAVY_IMPORTERS,
    "roles": ALLOWED_HEAVY_ROLE_IMPORTERS,
    "services": ALLOWED_HEAVY_SERVICE_IMPORTERS,
    "jobs": set(),
    "api": set(),
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
    paths = iter_root_modules() if package == ROOT_MODULES else iter_sources(package)
    assert paths, (
        f"no python sources found for '{package}' under any of {[str(r) for r in SRC_ROOTS]} — this guard "
        f"would pass vacuously. If the '{package}' package moved, it must have moved to a "
        f"packages/*/src/panel_core root.\n\n{SRC_ROOTS_DOC}"
    )
    return paths


def _discovered_packages():
    return sorted({ROOT_MODULES, *discovered_packages()})


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


def test_the_heavy_allowlists_still_point_at_real_files():
    stale = []
    for package, allowed in ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE.items():
        present = {path.name for path in _package_sources(package)}
        stale.extend(f"{package}/{name}" for name in sorted(allowed - present))
    assert stale == [], (
        f"ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE exempts files that no longer exist: {stale}. The allowlist "
        "matches on the base filename, so a stale entry is worse than useless after a package cut: it "
        "keeps exempting any future file with that name anywhere under the package (rglob), including a "
        "nested one. Move the entry with the file, or drop it."
    )


def test_every_panel_core_package_is_covered_by_the_layering_guard():
    discovered = _discovered_packages()
    assert NAMESPACE_PACKAGES <= set(discovered), (
        f"package discovery found {discovered}, missing {sorted(NAMESPACE_PACKAGES - set(discovered))} — "
        "every coverage claim below is vacuous for the packages it cannot see. Discovery must not depend "
        "on __init__.py: panel_core's api/ and services/ are namespace packages and have none, so an "
        "__init__.py-based scan silently drops exactly the two packages that matter most."
    )
    uncovered = [name for name in discovered if name not in ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE]
    assert uncovered == [], (
        f"panel_core packages with no layering guard: {uncovered}. Every package must have an entry in "
        "ALLOWED_HEAVY_IMPORTERS_BY_PACKAGE (an empty set means 'nothing here may be heavy'), otherwise a "
        f"new package silently escapes the check the way 'services' did. {HEAVY_ROOTS_DOC}"
    )


def test_xray_seam_imports_without_heavy_dependencies():
    probe = textwrap.dedent(_SEAM_IMPORT_PROBE).format(
        roots_path=[str(root) for root in SRC_ROOTS], roots=list(HEAVY_ROOTS)
    )
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
        matches = [path for path in iter_sources("xray") if path.name == name]
        assert len(matches) == 1, (
            f"expected exactly one xray/{name} across {[str(r) for r in SRC_ROOTS]}, found "
            f"{[str(m) for m in matches]}\n\n{SRC_ROOTS_DOC}"
        )
        path = matches[0]
        offenders = sorted({mod for mod in imported_modules(path) if heavy_root(mod, extra=("subprocess",))})
        assert offenders == [], (
            f"xray/{name} must stay importable anywhere — it is the pure part of the seam — but it imports "
            f"{offenders}. subprocess counts as heavy here: shelling out to the xray binary belongs in "
            f"engine.py. {HEAVY_ROOTS_DOC}"
        )
