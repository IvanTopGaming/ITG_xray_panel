import subprocess
import sys
import textwrap

HEAVY = ["docker", "grpc", "filelock", "app.proxyman", "app.stats", "common", "proxy"]


def _probe(import_line):
    script = textwrap.dedent(
        f"""
        import sys
        {import_line}
        heavy = [name for name in {HEAVY!r} if name in sys.modules]
        print(",".join(heavy))
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


def test_importing_panel_core_pulls_nothing_heavy():
    assert _probe("import panel_core") == []


def test_importing_app_base_pulls_nothing_heavy():
    assert _probe("import panel_core.app_base") == []


def test_light_role_modules_pull_nothing_heavy():
    assert _probe("import panel_core.roles.sub") == []
    assert _probe("import panel_core.roles.botapi") == []
