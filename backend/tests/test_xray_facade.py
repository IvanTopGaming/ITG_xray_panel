import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "packages/panel-core/src/panel_core"

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


def _shim_imports_from_the_package_root(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = "." * node.level + (node.module or "")
        if module not in ("panel_core.xray", ".xray", "..xray"):
            continue
        for alias in node.names:
            if alias.name in FACADE_NAMES:
                yield f"{path.name}:{node.lineno} -> {alias.name}"


def test_no_module_imports_the_shims_from_the_package_root():
    roots = (SRC, Path(__file__).resolve().parent)
    offenders = [
        hit
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for hit in _shim_imports_from_the_package_root(path)
    ]
    assert offenders == [], (
        "these modules still import gateway shims from the package root, which cannot survive "
        f"panel_core.xray becoming a namespace package: {offenders}. Relative forms ('from .xray import "
        "generate_config_file') break exactly the same way - app_base.py used two of them and the "
        "absolute-only version of this guard missed both."
    )
