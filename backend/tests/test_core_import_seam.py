import json
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


NAMESPACE_ROOT_WHY = (
    "panel_core is a namespace package, so importing the root must execute no code and must not drag "
    "any panel_core.* submodule in with it. Checking that the root pulls no *heavy* root was vacuous - a "
    "namespace root runs nothing, so nothing heavy could ever leak through it, and the assertion stayed "
    "green under an `import docker` mutation that turned both of its siblings red. What is worth pinning "
    "is the property the phase actually created: what the deleted __init__.py used to import (bootstrap, "
    "dispatch, xray.facade) is now reached only by naming those modules, so every entry point owns its "
    "own gevent bootstrap and no consumer can rely on an import side effect that a package cut removes."
)


def test_importing_the_namespace_root_executes_no_code():
    script = textwrap.dedent(
        """
        import json
        import sys

        import panel_core

        print(json.dumps({
            "file": getattr(panel_core, "__file__", None),
            "submodules": sorted(n for n in sys.modules if n.startswith("panel_core.")),
        }))
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])

    assert report["file"] is None, (
        f"panel_core resolved to a module file ({report['file']}), so it owns an __init__.py again and is "
        f"a regular package.\n\n{NAMESPACE_ROOT_WHY}"
    )
    assert report["submodules"] == [], (
        f"importing panel_core alone pulled these submodules into sys.modules: {report['submodules']}"
        f"\n\n{NAMESPACE_ROOT_WHY}"
    )


def test_importing_app_base_pulls_nothing_heavy():
    _assert_clean("import panel_core.app_base")


def test_light_role_modules_pull_nothing_heavy():
    _assert_clean("import panel_core.roles.sub")
    _assert_clean("import panel_core.roles.botapi")
