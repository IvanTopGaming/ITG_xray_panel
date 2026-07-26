import pytest

from tests.import_graph import SRC_ROOTS, SRC_ROOTS_DOC, iter_root_modules, iter_sources

GUARDED_DIRECTORIES = ["api", "services", "jobs", "roles", "xray"]


@pytest.mark.parametrize("name", GUARDED_DIRECTORIES)
def test_guarded_directory_exists_and_is_not_empty(name):
    sources = iter_sources(name)
    assert sources, (
        f"guarded package has no python files under any of {[str(r) for r in SRC_ROOTS]}: {name}\n\n{SRC_ROOTS_DOC}"
    )


def test_the_package_root_still_carries_modules():
    assert iter_root_modules(), (
        f"no top-level panel_core modules under any of {[str(r) for r in SRC_ROOTS]} — every guard "
        f"parameterised over the package root would pass vacuously.\n\n{SRC_ROOTS_DOC}"
    )
