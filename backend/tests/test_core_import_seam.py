import subprocess
import sys
import textwrap

from tests.import_graph import HEAVY_ROOTS, HEAVY_ROOTS_DOC


def _probe(import_line):
    script = textwrap.dedent(
        f"""
        import sys
        {import_line}
        roots = {list(HEAVY_ROOTS)!r}
        leaked = {{name.split(".")[0] for name in sys.modules}} & set(roots)
        print(",".join(sorted(leaked)))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return [name for name in result.stdout.strip().split(",") if name]


def _assert_clean(import_line):
    leaked = _probe(import_line)
    assert leaked == [], f"`{import_line}` pulled heavy roots into sys.modules: {leaked}\n\n{HEAVY_ROOTS_DOC}"


def test_importing_panel_core_pulls_nothing_heavy():
    _assert_clean("import panel_core")


def test_importing_app_base_pulls_nothing_heavy():
    _assert_clean("import panel_core.app_base")


def test_light_role_modules_pull_nothing_heavy():
    _assert_clean("import panel_core.roles.sub")
    _assert_clean("import panel_core.roles.botapi")
