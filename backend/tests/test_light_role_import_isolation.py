import os
import subprocess
import sys

import pytest

from tests.import_graph import HEAVY_ROOTS, HEAVY_ROOTS_DOC

LIGHT_ROLES = ("master", "sub", "botapi")
PANEL_ROLE_BY_MODULE = {"master": "master", "sub": "sub", "botapi": "bot"}
HEAVY_MODULE_ROOTS = HEAVY_ROOTS
MISSING_STUBS_MARKER = "No module named 'app'"

PROBE = """
import sys
import urllib.request
urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(
    OSError("network disabled for the isolation probe")
)
from panel_core.roles.{module} import create_app
create_app()
leaked = sorted({{name for name in sys.modules if name.split('.')[0] in {roots!r}}})
print("LEAKED:" + ",".join(leaked))
"""

BASE_ENV = {
    "SECRET_KEY": "x" * 40,
    "PANEL_DOMAIN": "localhost",
    "PROXY_DOMAIN": "localhost",
    "PANEL_SECRET_PATH": "/test",
    "PANEL_ADMIN_USER": "admin",
    "PANEL_ADMIN_PASSWORD": "admin",
    "RATELIMIT_STORAGE_URI": "memory://",
}
PROBE_TIMEOUT_SECONDS = 60

HINT = (
    "A light role regained a heavy import. The master, sub and bot-api must be able to fully boot "
    "(create_app(), not just `import` the role module) without grpcio, protobuf stubs, docker or "
    "filelock -- that is what makes the package cut possible. A bare `import panel_core.roles.<role>` "
    "is not enough to catch this: lazy imports inside blueprint registration (e.g. api/inbound.py "
    "pulling in services/stats.py, which pulls in grpc_client) only fire once create_app() actually "
    "registers the blueprint. Check what the role's module graph pulls in when it boots, not just on "
    f"bare import.\n\n{HEAVY_ROOTS_DOC}"
)


@pytest.mark.parametrize("role", LIGHT_ROLES)
def test_light_role_boots_without_heavy_dependencies(role, tmp_path):
    env = {
        **os.environ,
        **BASE_ENV,
        "PANEL_ROLE": PANEL_ROLE_BY_MODULE[role],
        "DATABASE_URL": f"sqlite:///{tmp_path}/{role}.db",
    }
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(module=role, roots=HEAVY_MODULE_ROOTS)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=PROBE_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, f"booting panel_core.roles.{role} failed:\n{result.stderr}\n\n{HINT}"

    leaked_line = [line for line in result.stdout.splitlines() if line.startswith("LEAKED:")]
    assert leaked_line, f"probe produced no marker; stdout={result.stdout!r} stderr={result.stderr!r}"
    leaked = [name for name in leaked_line[0][len("LEAKED:") :].split(",") if name]
    assert leaked == [], f"panel_core.roles.{role} pulled heavy modules while booting: {leaked}\n\n{HINT}"


REVOKE_PAYMENT_ACCESS_PROBE = f"""
import sys
import urllib.request
urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(
    OSError("network disabled for the isolation probe")
)
from panel_core.roles.master import create_app
app = create_app()
from panel_core.extensions import db
with app.app_context():
    db.create_all()
    from panel_core.services.provisioning import revoke_payment_access
    result = revoke_payment_access(999999999, 999999999)
print("RESULT:" + repr(result))
leaked = sorted({{name for name in sys.modules if name.split(".")[0] in {HEAVY_MODULE_ROOTS!r}}})
print("LEAKED:" + ",".join(leaked))
"""

REVOKE_PAYMENT_ACCESS_HINT = (
    "revoke_payment_access() must reach _api_remove_user_grpc via panel_core.xray, not "
    "panel_core.services.stats -- the latter imports panel_core.xray.grpc_client at module scope, "
    "which pulls in the generated protobuf stubs (app.proxyman.*, app.stats.*) that only exist inside "
    "the worker's Docker image. On the master role (no local Xray, no stubs) that import blows up the "
    "moment a YooKassa refund is processed -- via the hourly reconcile_refunds cron or the unsigned "
    "webhook, both of which run on this role."
)


def test_revoke_payment_access_does_not_pull_in_the_worker_grpc_stack(tmp_path):
    env = {
        **os.environ,
        **BASE_ENV,
        "PANEL_ROLE": "master",
        "DATABASE_URL": f"sqlite:///{tmp_path}/revoke.db",
    }
    result = subprocess.run(
        [sys.executable, "-c", REVOKE_PAYMENT_ACCESS_PROBE],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=PROBE_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, (
        f"revoke_payment_access() blew up on the master role:\n{result.stderr}\n\n{REVOKE_PAYMENT_ACCESS_HINT}"
    )
    assert "RESULT:" in result.stdout, f"probe produced no result marker; stdout={result.stdout!r}"

    leaked_line = [line for line in result.stdout.splitlines() if line.startswith("LEAKED:")]
    assert leaked_line, f"probe produced no leak marker; stdout={result.stdout!r} stderr={result.stderr!r}"
    leaked = [name for name in leaked_line[0][len("LEAKED:") :].split(",") if name]
    assert leaked == [], (
        f"revoke_payment_access() pulled heavy modules on the master role: {leaked}\n\n{REVOKE_PAYMENT_ACCESS_HINT}"
    )


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
