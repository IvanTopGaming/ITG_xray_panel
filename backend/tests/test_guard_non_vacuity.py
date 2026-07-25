import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "packages" / "panel-core" / "src" / "panel_core"

GUARDED_DIRECTORIES = ["api", "services", "jobs", "roles", "xray"]


@pytest.mark.parametrize("name", GUARDED_DIRECTORIES)
def test_guarded_directory_exists_and_is_not_empty(name):
    directory = SRC / name
    assert directory.is_dir(), f"guarded directory missing: {name}"
    assert list(directory.rglob("*.py")), f"guarded directory has no python files: {name}"
