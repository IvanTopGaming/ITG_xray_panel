import ast
from pathlib import Path

from tests.import_graph import BACKEND, SRC_ROOTS, SRC_ROOTS_DOC, iter_sources

ENTRY_POINT_SCRIPTS = ("run.py", "migrate_db.py", "sqlite_to_pg.py")

FACADE_NAMES = (
    "has_local_xray",
    "generate_config_file",
    "restart_xray_container",
    "stream_xray_logs",
    "update_geo_db",
    "_api_add_user_grpc",
    "_api_remove_user_grpc",
)


def test_facade_module_exposes_every_shim():
    from panel_core.xray import facade

    for name in FACADE_NAMES:
        assert hasattr(facade, name), f"facade is missing {name}"


def test_the_package_root_carries_no_re_exports_to_import():
    import panel_core.xray

    assert getattr(panel_core.xray, "__file__", None) is None, (
        "panel_core.xray is a regular package again: "
        f"{getattr(panel_core.xray, '__file__', None)}. A namespace package owns no __init__.py, so it "
        "cannot re-export anything - every consumer must import from panel_core.xray.facade."
    )
    for name in FACADE_NAMES:
        assert not hasattr(panel_core.xray, name), (
            f"{name} is reachable on the package root, so something re-created the shim layer that "
            "namespace packaging removed"
        )


PACKAGE_ROOT_MODULES = ("panel_core.xray", ".xray", "..xray")


def _dotted_name(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _package_root_aliases(tree):
    aliases = {"panel_core.xray"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "panel_core.xray" and alias.asname:
                    aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            if module not in ("panel_core", ".", ".."):
                continue
            for alias in node.names:
                if alias.name == "xray":
                    aliases.add(alias.asname or "xray")
    return aliases


def _shim_imports_from_the_package_root(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = "." * node.level + (node.module or "")
        if module not in PACKAGE_ROOT_MODULES:
            continue
        for alias in node.names:
            if alias.name in FACADE_NAMES:
                yield f"{path.name}:{node.lineno} -> from {module} import {alias.name}"

    aliases = _package_root_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in FACADE_NAMES:
            continue
        target = _dotted_name(node.value)
        if target in aliases:
            yield f"{path.name}:{node.lineno} -> {target}.{node.attr}"


def _scanned_paths():
    package_sources = iter_sources()
    assert package_sources, (
        f"no python sources found under any of {[str(r) for r in SRC_ROOTS]} — this guard would pass "
        f"vacuously.\n\n{SRC_ROOTS_DOC}"
    )
    paths = package_sources + sorted(Path(__file__).resolve().parent.rglob("*.py"))
    scripts = [BACKEND / name for name in ENTRY_POINT_SCRIPTS]
    missing = [script.name for script in scripts if not script.is_file()]
    assert missing == [], (
        f"entry-point scripts declared in ENTRY_POINT_SCRIPTS are gone: {missing}. They live outside the "
        "package, so nothing else scans them - a stale name here silently shrinks this guard's reach."
    )
    return paths + scripts


def test_no_module_imports_the_shims_from_the_package_root():
    offenders = [hit for path in _scanned_paths() for hit in _shim_imports_from_the_package_root(path)]
    assert offenders == [], (
        "these modules still reach gateway shims through the package root, which cannot survive "
        f"panel_core.xray becoming a namespace package: {offenders}. Relative forms ('from .xray import "
        "generate_config_file') break exactly the same way - app_base.py used two of them and the "
        "absolute-only version of this guard missed both. So does the attribute form ('from panel_core "
        "import xray' then 'xray.generate_config_file(...)'): it resolves the same attribute on the same "
        "package root, just one statement later. Import from panel_core.xray.facade instead."
    )
