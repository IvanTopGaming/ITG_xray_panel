from pathlib import Path

import pytest

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


def test_the_app_builds_when_the_namespace_spans_two_distributions(tmp_path, monkeypatch):
    import panel_core

    from panel_core.app_base import INSTANCE_PATH, PACKAGE_ROOT

    second = tmp_path / "panel_core"
    second.mkdir()
    monkeypatch.setattr(
        panel_core.__spec__,
        "submodule_search_locations",
        [*panel_core.__path__, str(second)],
    )

    from flask.sansio.scaffold import _find_package_path

    with pytest.raises(StopIteration):
        _find_package_path("panel_core")

    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("PANEL_DOMAIN", "localhost")
    monkeypatch.setenv("PANEL_SECRET_PATH", "/test")
    monkeypatch.setenv("PANEL_ADMIN_USER", "admin")
    monkeypatch.setenv("PANEL_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "memory://")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/spread.db")

    from panel_core.dispatch import create_app

    try:
        app = create_app()
    except StopIteration:
        raise AssertionError(
            "create_app() could not build with panel_core spanning two distributions. Neither root_path "
            "nor instance_path may be left to Flask's auto-discovery: it derives root_path from __file__ "
            "(a namespace package has none) and instance_path from import_name via _find_package_path, "
            "whose namespace branch does a bare next() over the search locations and raises StopIteration "
            "once more than one location contributes. Both must be passed explicitly in build_base_app."
        ) from None
    finally:
        from panel_core.extensions import scheduler

        scheduler.remove_all_jobs()
        if scheduler.running:
            scheduler.shutdown(wait=False)

    assert app.root_path == PACKAGE_ROOT
    assert app.instance_path == INSTANCE_PATH
