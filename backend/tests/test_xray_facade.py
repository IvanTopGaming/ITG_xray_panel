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


def test_package_root_still_re_exports_the_same_objects():
    from panel_core import xray
    from panel_core.xray import facade

    for name in FACADE_NAMES:
        assert getattr(xray, name) is getattr(facade, name), f"{name} must be re-exported, not redefined"


def test_no_module_imports_the_shims_from_the_package_root():
    roots = (SRC, Path(__file__).resolve().parent)
    offenders = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path.name == "__init__.py" and path.parent.name == "xray":
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.module != "panel_core.xray":
                    continue
                for alias in node.names:
                    if alias.name in FACADE_NAMES:
                        offenders.append(f"{path.name} -> {alias.name}")
    assert offenders == [], (
        "these modules still import gateway shims from the package root, which cannot survive "
        f"panel_core.xray becoming a namespace package: {offenders}"
    )
