"""The installer's one job: a host that comes up on the first `docker compose up -d`.

Every compose file states its hard requirements as `${VAR:?...}` -- a value the stack refuses to
start without. Until now a deployer met them by copying an example and filling twenty blanks by
hand, seven of which have to match byte-for-byte across machines. That is the step this script
replaces, so the property worth guarding is exactly the contract between the two: **for every role,
the generated .env satisfies every `${VAR:?}` its own compose file demands, with a non-empty value.**

The tests drive the script through `--non-interactive --no-start --source .`, which is also the mode
CI and the suite use: no prompts, no `docker compose up`, and the compose files read from this
checkout instead of being fetched from GitHub. That last flag is not test scaffolding -- it is how
you install from a clone.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "install.sh"

REQUIRED_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*):\?")

COMPOSE_BY_ROLE = {
    "data": "docker-compose.postgres.yml",
    "cron": "docker-compose.cron.yml",
    "master": "docker-compose.master.yml",
    "node": "docker-compose.node.yml",
    "sub": "docker-compose.sub.yml",
    "bot": "docker-compose.bot.yml",
}

ANSWERS = {
    "data": {"DATA_HOSTNAME": "data.example.com"},
    "cron": {},
    "master": {"PANEL_DOMAIN": "panel.example.com", "SUB_DOMAIN": "sub.example.com"},
    "node": {
        "PANEL_DOMAIN": "node1.example.com",
        "SUB_DOMAIN": "sub.example.com",
        "PROXY_DOMAIN": "www.google.com",
    },
    "sub": {"SUB_DOMAIN": "sub.example.com", "PANEL_DOMAIN": "panel.example.com"},
    "bot": {
        "BOT_DOMAIN": "bot.example.com",
        "SUB_DOMAIN": "sub.example.com",
        "PANEL_DOMAIN": "panel.example.com",
    },
}


def _run(role, target, bundle=None, answers=None):
    env = dict(os.environ)
    env.update(answers or ANSWERS[role])
    if bundle:
        env["BUNDLE"] = bundle
    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--role",
            role,
            "--dir",
            str(target),
            "--source",
            str(REPO),
            "--non-interactive",
            "--no-start",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(target),
    )
    assert result.returncode == 0, (
        f"installer failed for role {role}:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _env_values(path):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value
    return values


def _bundle_from(stdout):
    for line in stdout.splitlines():
        candidate = line.strip()
        if len(candidate) > 40 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", candidate):
            try:
                json.loads(base64.urlsafe_b64decode(candidate + "=" * (-len(candidate) % 4)))
            except Exception:
                continue
            return candidate
    raise AssertionError(f"no bundle found in installer output:\n{stdout}")


def test_the_installer_exists_and_is_executable():
    assert INSTALLER.is_file(), (
        "scripts/install.sh is missing. It is the entry point the README tells a deployer to pipe "
        "into bash, and every other test in this file drives it."
    )


@pytest.mark.parametrize("role", sorted(COMPOSE_BY_ROLE))
def test_every_hard_requirement_of_that_role_is_filled_in(role, tmp_path):
    """A `${VAR:?}` with no value is a stack that dies on `up` with a bare compose error."""

    target = tmp_path / role
    target.mkdir()

    bundle = None
    if role != "data":
        data_dir = tmp_path / f"data-{role}"
        data_dir.mkdir()
        bundle = _bundle_from(_run("data", data_dir).stdout)

    _run(role, target, bundle=bundle)

    compose = REPO / COMPOSE_BY_ROLE[role]
    required = set(REQUIRED_REF.findall(compose.read_text()))
    assert required, f"{compose.name} demands no ${{VAR:?}} at all — this guard would pass vacuously"

    values = _env_values(target / ".env")
    missing = sorted(v for v in required if not values.get(v))
    assert missing == [], (
        f"the installer left {missing} empty for role {role}, and {compose.name} refuses to start "
        f"without them. Generated .env:\n{(target / '.env').read_text()}"
    )


CA_IN_CONTAINER = "/etc/ssl/panel-ca.crt"

COMPOSE_FILES = sorted({name for name in COMPOSE_BY_ROLE.values() if name != "docker-compose.postgres.yml"})


def _services_reaching_the_data_tier(compose):
    import yaml

    document = yaml.safe_load((REPO / compose).read_text()) or {}
    out = {}
    for name, definition in (document.get("services") or {}).items():
        if not isinstance(definition, dict):
            continue
        env = definition.get("environment") or []
        entries = env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]
        if any(e.split("=")[0] in {"DATABASE_URL", "SHARED_REDIS_URI", "BOT_SHARED_REDIS_URI"} for e in entries):
            out[name] = definition
    return out


@pytest.mark.parametrize("compose", COMPOSE_FILES)
def test_every_service_that_talks_to_the_data_tier_can_verify_its_certificate(compose):
    """The data tier presents a certificate from its own CA, so its clients need that CA in the image.

    Both hops are verified TLS and neither can fall back: Postgres is pinned to `sslmode=verify-full`
    by db_config, and redis-py defaults `rediss://` to CERT_REQUIRED. The CA is not in any container's
    trust store -- it is generated per deployment on the data VM -- so it has to be mounted, and the
    URIs point at exactly this path. Without the mount every one of these services starts and then
    fails every connection with "certificate verify failed", which reads like a password problem.
    """

    services = _services_reaching_the_data_tier(compose)
    assert services, f"{compose} has no service reaching the data tier — this guard would pass vacuously"

    missing = []
    for name, definition in services.items():
        volumes = definition.get("volumes") or []
        if not any(CA_IN_CONTAINER in str(v) for v in volumes):
            missing.append(name)

    assert missing == [], (
        f"{compose}: {missing} read DATABASE_URL or a shared-Redis URI but never mount the data tier's "
        f"CA at {CA_IN_CONTAINER}. The installer writes ./ca.crt next to the compose file and points "
        f"sslrootcert / ssl_ca_certs there."
    )
