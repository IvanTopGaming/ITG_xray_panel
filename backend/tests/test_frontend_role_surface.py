import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
UI_CORE = REPO / "frontend" / "packages" / "ui-core" / "src"

MASTER_ONLY_PATHS = {
    "/panels": "the `panels` blueprint, registered by roles/master.py and not by roles/worker.py",
    "/bot/": "the `bot_admin` blueprint, which ships from panel-master and is not installed on a node",
}

GATE_DOC = (
    "ui-core is shared by the admin and node apps, so any call it makes must either work on a node or "
    "be gated on the role. `panels` and `bot_admin` are master-only: roles/worker.py registers neither, "
    "and both modules ship from panel-master, so on a node these paths 404 on every session — silently, "
    "because nothing surfaces a failed background query. Put the call behind a single gated hook rather "
    "than repeating an `enabled:` at each call site, so this guard stays checkable."
)


def _ui_core_sources():
    paths = sorted(p for p in UI_CORE.rglob("*.ts*") if p.suffix in (".ts", ".tsx"))
    assert len(paths) > 20, (
        f"only {len(paths)} sources found under {UI_CORE} — the frontend layout moved and this guard "
        f"would pass vacuously.\n\n{GATE_DOC}"
    )
    return paths


@pytest.mark.parametrize("api_path", sorted(MASTER_ONLY_PATHS))
def test_a_master_only_path_is_called_from_one_gated_place_in_ui_core(api_path):
    pattern = re.compile(rf"""api\.\w+[^(\n]*\(\s*['"`]{re.escape(api_path)}""")
    callers = [p for p in _ui_core_sources() if pattern.search(p.read_text())]

    assert len(callers) <= 1, (
        f"{api_path} ({MASTER_ONLY_PATHS[api_path]}) is called from "
        f"{[str(p.relative_to(REPO)) for p in callers]}. Exactly one ui-core module may reach it, and "
        f"that module must gate on the role.\n\n{GATE_DOC}"
    )

    for caller in callers:
        assert "isWorker" in caller.read_text(), (
            f"{caller.relative_to(REPO)} calls {api_path} without referencing isWorker — the query must "
            f"carry `enabled: !isWorker`.\n\n{GATE_DOC}"
        )
