import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
FRONTEND_PACKAGES = REPO / "frontend" / "packages"

ADMIN_ONLY_PACKAGES = {"admin"}

MASTER_ONLY_PATHS = {
    "/panels": "the `panels` blueprint, registered by roles/master.py and not by roles/worker.py",
    "/bot/": "the `bot_admin` blueprint, which ships from panel-master and is not installed on a node",
}

GATE_DOC = (
    "ui-core is shared by the admin and node apps, and packages/node/src ships into the node image "
    "right alongside it — a call from either must work on a node or be gated on the role. `panels` and "
    "`bot_admin` are master-only: roles/worker.py registers neither, and both modules ship from "
    "panel-master, so on a node these paths 404 on every session — silently, because nothing surfaces "
    "a failed background query. Put the call behind a single gated hook rather than repeating an "
    "`enabled:` at each call site, so this guard stays checkable."
)


def _node_image_package_names():
    names = sorted(
        p.name
        for p in FRONTEND_PACKAGES.iterdir()
        if p.is_dir() and p.name not in ADMIN_ONLY_PACKAGES and (p / "src").is_dir()
    )
    assert names, (
        f"no non-admin package with a src directory found under {FRONTEND_PACKAGES} — the workspace "
        f"layout moved and this guard would pass vacuously.\n\n{GATE_DOC}"
    )
    return names


def _node_image_sources():
    paths = []
    for name in _node_image_package_names():
        root = FRONTEND_PACKAGES / name / "src"
        paths.extend(p for p in root.rglob("*.ts*") if p.suffix in (".ts", ".tsx"))
    paths = sorted(paths)
    assert len(paths) > 20, (
        f"only {len(paths)} sources found across {_node_image_package_names()} — the frontend layout "
        f"moved and this guard would pass vacuously.\n\n{GATE_DOC}"
    )
    return paths


@pytest.mark.parametrize("api_path", sorted(MASTER_ONLY_PATHS))
def test_a_master_only_path_is_called_from_one_gated_place_in_the_node_image(api_path):
    pattern = re.compile(rf"""api\.\w+[^(\n]*\(\s*['"`]{re.escape(api_path)}""")
    callers = [p for p in _node_image_sources() if pattern.search(p.read_text())]

    assert len(callers) <= 1, (
        f"{api_path} ({MASTER_ONLY_PATHS[api_path]}) is called from "
        f"{[str(p.relative_to(REPO)) for p in callers]}. Exactly one module across the node image "
        f"(ui-core + node) may reach it, and that module must gate on the role.\n\n{GATE_DOC}"
    )

    for caller in callers:
        assert "!isWorker" in caller.read_text(), (
            f"{caller.relative_to(REPO)} calls {api_path} without a `!isWorker` gate — the query must "
            f"carry `enabled: !isWorker`. Matching the negated form on purpose: a bare `isWorker` check "
            f"passes when the gate is deleted but the symbol survives anywhere else in the file, which "
            f"is the exact regression this guard exists to catch.\n\n{GATE_DOC}"
        )
