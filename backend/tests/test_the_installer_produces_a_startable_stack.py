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
        # the rendered .env keeps a trailing note after the value; the value is what precedes it
        values[key.strip()] = re.split(r"\s+#", value, maxsplit=1)[0].strip()
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


@pytest.mark.parametrize(
    ("address", "expected_san"),
    [("data.internal", "DNS:data.internal"), ("10.0.0.10", "IP Address:10.0.0.10")],
)
def test_the_data_tier_certificate_matches_how_it_will_be_dialled(address, expected_san, tmp_path):
    """An IP belongs in an iPAddress SAN, a name in a DNS one, and OpenSSL never crosses the two.

    Every client reaches the data tier with verify-full (Postgres) or CERT_REQUIRED (redis-py), both
    of which check the address dialled against the certificate. Deployments without internal DNS
    reasonably answer the installer's prompt with a private IP, and issuing DNS:10.0.0.10 for that
    produces a certificate that fails every connection with a name mismatch -- an error that reads
    like wrong credentials, on a host where the credentials are fine.
    """

    target = tmp_path / "data"
    target.mkdir()
    _run("data", target, answers={"DATA_HOSTNAME": address})

    text = subprocess.run(
        ["openssl", "x509", "-in", str(target / "pg_certs" / "server.crt"), "-noout", "-text"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert expected_san in text, (
        f"the certificate for {address} carries no {expected_san}; OpenSSL matches an IP only "
        f"against iPAddress entries and a name only against DNS ones.\n\n{text}"
    )


def _fresh_bundle(tmp_path, tag):
    data_dir = tmp_path / f"data-{tag}"
    data_dir.mkdir()
    return _bundle_from(_run("data", data_dir).stdout)


def _cmd(command, target, *extra, expect_ok=True):
    result = subprocess.run(
        ["bash", str(INSTALLER), command, "--dir", str(target), "--source", str(REPO), *extra],
        capture_output=True,
        text=True,
        env={**os.environ, "NO_COLOR": "1"},
        cwd=str(target),
    )
    if expect_ok:
        assert result.returncode == 0, (
            f"`install.sh {command}` failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def test_doctor_reports_on_an_installed_host(tmp_path):
    """After the install the deployer's next question is always 'is it actually working?'.

    Answering it by hand means remembering which compose file this host uses, which containers
    should exist, whether the data tier is reachable and whether the pins are current -- on six
    machines. doctor is that checklist, and it must run without docker present so it still says
    something useful on a half-provisioned box.
    """

    target = tmp_path / "master"
    target.mkdir()
    bundle = _fresh_bundle(tmp_path, "d")
    _run("master", target, bundle=bundle)

    out = _cmd("doctor", target).stdout

    assert "master" in out, f"doctor does not name the role it inspected:\n{out}"
    assert "data tier" in out.lower(), f"doctor says nothing about the data tier:\n{out}"


def test_update_is_a_no_op_when_the_pins_already_match(tmp_path):
    target = tmp_path / "cron"
    target.mkdir()
    bundle = _fresh_bundle(tmp_path, "d2")
    _run("cron", target, bundle=bundle)

    out = _cmd("update", target, "--no-start").stdout

    assert "up to date" in out.lower(), f"update did not recognise an already-current host:\n{out}"


def test_update_rewrites_only_the_pins_that_moved(tmp_path):
    """An update must not touch anything but the image tags -- the secrets in .env are irreplaceable."""

    target = tmp_path / "cron"
    target.mkdir()
    bundle = _fresh_bundle(tmp_path, "d3")
    _run("cron", target, bundle=bundle)

    before = _env_values(target / ".env")
    stale = (target / ".env").read_text().replace(before["CRON_IMAGE"], "ghcr.io/ivantopgaming/panel-cron:v0.0.1")
    (target / ".env").write_text(stale)

    out = _cmd("update", target, "--no-start").stdout
    after = _env_values(target / ".env")

    assert after["CRON_IMAGE"] == before["CRON_IMAGE"], f"the pin was not brought forward:\n{out}"
    for key, value in before.items():
        if key != "CRON_IMAGE":
            assert after[key] == value, f"update changed {key}, which is not an image pin"


def test_a_second_install_refuses_rather_than_overwriting(tmp_path):
    target = tmp_path / "sub"
    target.mkdir()
    bundle = _fresh_bundle(tmp_path, "d4")
    _run("sub", target, bundle=bundle)
    secrets = _env_values(target / ".env")

    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--role",
            "sub",
            "--dir",
            str(target),
            "--source",
            str(REPO),
            "--non-interactive",
            "--no-start",
        ],
        capture_output=True,
        text=True,
        cwd=str(target),
        env={**os.environ, "NO_COLOR": "1", "BUNDLE": bundle, **ANSWERS["sub"]},
    )

    assert result.returncode != 0, "a second install overwrote a live deployment instead of refusing"
    assert _env_values(target / ".env") == secrets, "the existing .env was modified by the refused run"


def test_reconfigure_keeps_the_secrets_and_takes_new_domains(tmp_path):
    """Domains change -- a host moves, a domain is replaced. Secrets must survive that.

    Re-running the installer cannot be the answer: it would mint a new SECRET_KEY and a new admin
    password, and on the master that also means a new secret path, so every existing session and
    every bookmark breaks for what should be a one-line edit.
    """

    target = tmp_path / "sub"
    target.mkdir()
    bundle = _fresh_bundle(tmp_path, "d5")
    _run("sub", target, bundle=bundle)
    before = _env_values(target / ".env")

    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "reconfigure",
            "--dir",
            str(target),
            "--source",
            str(REPO),
            "--non-interactive",
            "--no-start",
        ],
        capture_output=True,
        text=True,
        cwd=str(target),
        env={**os.environ, "NO_COLOR": "1", "SUB_DOMAIN": "moved.example.com", "PANEL_DOMAIN": before["PANEL_DOMAIN"]},
    )
    assert result.returncode == 0, f"reconfigure failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    after = _env_values(target / ".env")
    assert after["SUB_DOMAIN"] == "moved.example.com", "reconfigure did not take the new domain"
    assert after["SECRET_KEY"] == before["SECRET_KEY"], "reconfigure rotated SECRET_KEY"
    assert after["DATABASE_URL"] == before["DATABASE_URL"], "reconfigure rewrote the data-tier URI"


def _file_shaped_bind_mounts(compose):
    import yaml

    document = yaml.safe_load((REPO / compose).read_text()) or {}
    out = set()
    for definition in (document.get("services") or {}).values():
        if not isinstance(definition, dict):
            continue
        for volume in definition.get("volumes") or []:
            if not isinstance(volume, str) or not volume.startswith("./"):
                continue
            source = volume.split(":", 1)[0]
            if "." in source.rsplit("/", 1)[-1]:
                out.add(source[2:])
    return sorted(out)


@pytest.mark.parametrize("role", sorted(COMPOSE_BY_ROLE))
def test_the_installer_delivers_every_file_the_compose_bind_mounts(role, tmp_path):
    target = tmp_path / role
    target.mkdir()

    bundle = None
    if role != "data":
        data_dir = tmp_path / f"data-{role}"
        data_dir.mkdir()
        bundle = _bundle_from(_run("data", data_dir).stdout)

    _run(role, target, bundle=bundle)

    expected = _file_shaped_bind_mounts(COMPOSE_BY_ROLE[role])
    assert expected, (
        f"{COMPOSE_BY_ROLE[role]} bind-mounts no file-shaped path at all, so this guard would pass "
        f"vacuously for role {role} -- the parser stopped matching the compose file's shape."
    )
    for relative in expected:
        path = target / relative
        assert path.is_file(), (
            f"role {role}: {COMPOSE_BY_ROLE[role]} bind-mounts ./{relative}, but the installer did "
            f"not put a file there. Docker does not fail on a missing bind-mount source -- it "
            f"silently creates a DIRECTORY, and whatever consumes the path fails in a way the "
            f"container survives. That is exactly how scripts/pg_backup.sh shipped: the pg-backup "
            f"entrypoint swallowed 'Is a directory' with `|| true`, the container sat `running` "
            f"forever, `install.sh doctor` reported it healthy, and the shared Postgres -- every "
            f"bot token, YooKassa key and federation token in the deployment -- had no backup at "
            f"all. Fetch or write the file in install.sh."
        )
