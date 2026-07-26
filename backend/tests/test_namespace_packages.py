from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "packages/panel-core/src/panel_core"

NAMESPACE_DIRS = ("", "api", "services", "jobs", "roles", "xray", "data")

HINT = (
    "A namespace package cannot carry __init__.py: the moment two distributions ship into the same "
    "directory, only the one owning __init__.py would be importable. Move whatever the file held "
    "into an explicit module, the way bootstrap.py, dispatch.py and xray/facade.py were extracted."
)


def test_split_packages_carry_no_init():
    offenders = [d or "panel_core" for d in NAMESPACE_DIRS if (SRC / d / "__init__.py").exists()]
    assert offenders == [], f"these must be namespace packages: {offenders}\n\n{HINT}"


def test_panel_core_imports_as_a_namespace_package():
    import panel_core

    assert getattr(panel_core, "__file__", None) is None, (
        "panel_core resolved to a module file, so it is still a regular package: "
        f"{getattr(panel_core, '__file__', None)}"
    )
    assert hasattr(panel_core, "__path__")


def test_the_extracted_modules_are_still_reachable():
    from panel_core.bootstrap import bootstrap_gevent
    from panel_core.dispatch import create_app
    from panel_core.xray.facade import has_local_xray

    assert callable(bootstrap_gevent)
    assert callable(create_app)
    assert callable(has_local_xray)
