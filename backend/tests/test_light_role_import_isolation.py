import subprocess
import sys

import pytest

LIGHT_ROLES = ("master", "sub", "botapi")
HEAVY_MODULE_ROOTS = ("grpc", "google", "docker", "filelock")
MISSING_STUBS_MARKER = "No module named 'app'"

PROBE = """
import sys
import {module}
leaked = sorted({{name for name in sys.modules if name.split('.')[0] in {roots!r}}})
print("LEAKED:" + ",".join(leaked))
"""

HINT = (
    "A light role regained a heavy import. The master, sub and bot-api must load without grpcio, "
    "protobuf stubs, docker or filelock -- that is what makes the package cut possible. "
    "Check what the role's module graph pulls in: the usual culprit is services/stats.py."
)


@pytest.mark.parametrize("role", LIGHT_ROLES)
def test_light_role_imports_without_heavy_dependencies(role):
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(module=f"panel_core.roles.{role}", roots=HEAVY_MODULE_ROOTS)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"importing panel_core.roles.{role} failed:\n{result.stderr}\n\n{HINT}"

    leaked_line = [line for line in result.stdout.splitlines() if line.startswith("LEAKED:")]
    assert leaked_line, f"probe produced no marker; stdout={result.stdout!r} stderr={result.stderr!r}"
    leaked = [name for name in leaked_line[0][len("LEAKED:") :].split(",") if name]
    assert leaked == [], f"panel_core.roles.{role} pulled heavy modules: {leaked}\n\n{HINT}"


def test_worker_role_still_needs_the_heavy_stack():
    result = subprocess.run(
        [sys.executable, "-c", "import panel_core.roles.worker"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "panel_core.roles.worker imported cleanly outside Docker, which means it no longer needs the "
        "protobuf stubs. If that is intentional, this guard is obsolete -- but verify it deliberately, "
        "because a silently-passing guard here would hide the very regression the light-role tests catch. "
        "The other way to land here is an environment that does have the stubs: generated protobuf output "
        "left in backend/app/, or pytest running inside the Docker image. Check that before touching the code."
    )
    assert MISSING_STUBS_MARKER in result.stderr, (
        f"panel_core.roles.worker failed for the wrong reason, so this guard no longer proves anything:\n"
        f"{result.stderr}\n\n"
        f"It must fail with {MISSING_STUBS_MARKER!r} -- that is the proof the subprocess runs without the "
        "protobuf stubs conftest.py injects, which is the only reason the light-role assertions above mean "
        "anything. A different error (typically the module being renamed or moved to another package during "
        "a package cut) still yields a non-zero exit code and would have kept this test vacuously green."
    )
