"""§64: a variable a host is made to set must be read by something inside that host's images.

`test_env_examples.py` guards the two *names* against each other: what a compose file demands and
what its example defines. It says nothing about whether anything on the other side of the container
boundary ever looks the variable up -- so `PANEL_SECRET_PATH` sat in `docker-compose.bot.yml` and
`docker-compose.sub.yml` as a hard `${VAR:?}` start-up requirement, with a comment in both example
files explaining that `services/sub_links` read it for the PANEL_DOMAIN fallback. Neither half was
true: the fallback was deleted in wave 3b, and the one module that reads that variable
(`api/federation.py`) ships from `panel-adminapi`, which is installed in neither image.

Wave 5a is about instructions that mislead a deployer. The prose half of that -- *why* a variable
matters -- is not mechanically checkable and stays a manual read. This half is: resolve each service
to the source tree that actually ends up in its image, and require the variable's name to appear
there. It is deliberately coarse (a name mentioned anywhere in the tree counts, because
`RATELIMIT_STORAGE_URI` is read through a module-level constant rather than a literal `os.getenv`),
which keeps it free of false alarms while still catching a variable no code has heard of at all.

**§87 (wave 5d): coarse is fine, reading your own tests is not.** This guard was green on
`docker-compose.bot.yml`'s `bot` service -- the Telegram poller, which has no limiter -- while it
demanded `RATELIMIT_STORAGE_URI` through a `${VAR:?}`, because the scan rooted at `tg_bot/` walked
into `tg_bot/tests/`, where `test_consumer_claim.py` sets that variable *to assert the consumer
ignores it*. The variable is gone from the compose file and test directories are gone from the scan;
`test_the_scan_reads_the_bot_image_and_not_its_test_suite` holds both ends of that exclusion.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = REPO_ROOT / "backend" / "packages"

WHY = (
    "Either the code that used to read it is gone (delete the variable from the compose file and from "
    "that host's .env.*.example), or it never lived in this image (the distribution that reads it is not "
    "in this role's dependency closure). A `${VAR:?…}` nothing reads is not harmless: it refuses the "
    "`up` until the deployer supplies a value, which is a promise that it does something."
)

# service name -> the source trees that end up inside that service's image.
# Only services built from this repo are checked; redis/postgres/xray/socket-proxy read their own.
ROLE_SERVICES = {
    ("docker-compose.master.yml", "backend"): "master",
    ("docker-compose.node.yml", "backend"): "worker",
    ("docker-compose.sub.yml", "sub-backend"): "sub",
    ("docker-compose.bot.yml", "bot-api"): "botapi",
    ("docker-compose.cron.yml", "cron"): "cron",
}

ROLE_DISTRIBUTIONS = {
    "master": "panel-master",
    "worker": "panel-worker",
    "sub": "panel-sub",
    "botapi": "panel-botapi",
    "cron": "panel-cron",
}

PLAIN_SERVICES = {
    ("docker-compose.bot.yml", "bot"): [REPO_ROOT / "tg_bot"],
    ("docker-compose.master.yml", "frontend"): [
        REPO_ROOT / "frontend" / "entrypoint.sh",
        REPO_ROOT / "frontend" / "nginx.conf.template",
    ],
    ("docker-compose.node.yml", "frontend"): [
        REPO_ROOT / "frontend" / "entrypoint.sh",
        REPO_ROOT / "frontend" / "nginx.conf.template",
    ],
    ("docker-compose.master.yml", "caddy"): [REPO_ROOT / "caddy"],
    ("docker-compose.node.yml", "caddy"): [REPO_ROOT / "caddy"],
    ("docker-compose.sub.yml", "caddy"): [REPO_ROOT / "caddy"],
    ("docker-compose.bot.yml", "caddy"): [REPO_ROOT / "caddy"],
}

# Consumed by the interpreter, gRPC or the base image, with no line of ours to point at.
RUNTIME_VARIABLES = {
    "PYTHONUNBUFFERED",
    "GRPC_DNS_RESOLVER",
    "DOCKER_HOST",  # read by the `docker` SDK itself, which only panel-worker installs
    "TZ",
}

# §87: a test is not the code inside the image. `tg_bot/tests/test_consumer_claim.py` sets
# RATELIMIT_STORAGE_URI *to prove the consumer ignores it*, and that one line kept this guard green
# while `docker-compose.bot.yml` demanded the variable from the Telegram poller, which has no
# limiter and never looks it up. A guard that reads its own test suite grades itself.
# The role services never had the hole -- their roots are `packages/<dist>/src`, which `backend/tests`
# is outside of -- but the exclusion is applied to every root, so a future PLAIN_SERVICES entry
# rooted at a package directory cannot re-open it.
_SKIPPED_DIRECTORIES = {"__pycache__", "node_modules", "tests", "test"}

SERVICE_RE = re.compile(r"^  ([A-Za-z0-9_-]+):$")
ENV_ENTRY_RE = re.compile(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)=")


def _service_blocks(compose_name):

    blocks, current, lines = {}, None, []
    for line in (REPO_ROOT / compose_name).read_text().splitlines():
        match = SERVICE_RE.match(line)
        if match:
            if current:
                blocks[current] = "\n".join(lines)
            current, lines = match.group(1), []
            continue
        if current:
            lines.append(line)
    if current:
        blocks[current] = "\n".join(lines)
    return blocks


def _environment_variables(block):
    """The names on the LEFT of each `environment:` entry — those are what the container sees.

    Not the `${...}` on the right: `docker-compose.master.yml` deliberately renames some on the way in
    (`PANEL_USER=${PANEL_ADMIN_USER}`), and it is the left-hand name the code looks up. `image:`,
    `ports:`, volume paths and `healthcheck:` are substituted on the host, so a variable used only
    there is compose plumbing and makes no claim about the code.
    """

    wanted, inside = set(), False
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("environment:"):
            inside = True
            continue
        if inside and stripped and not stripped.startswith(("-", "#")):
            break
        match = ENV_ENTRY_RE.match(line) if inside else None
        if match:
            wanted.add(match.group(1))
    return wanted - RUNTIME_VARIABLES


def _dependency_closure(distribution):

    seen, queue = set(), [distribution]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        pyproject = PACKAGES / name / "pyproject.toml"
        if not pyproject.is_file():
            continue
        seen.add(name)
        data = tomllib.loads(pyproject.read_text())
        for raw in data.get("project", {}).get("dependencies", []):
            dep = re.split(r"[<>=!\[ ]", raw, maxsplit=1)[0].strip()
            if (PACKAGES / dep).is_dir():
                queue.append(dep)
    return seen


def _source_text(roots):

    chunks = []
    for root in roots:
        if root.is_file():
            chunks.append(root.read_text(errors="ignore"))
            continue
        for path in root.rglob("*"):
            if not path.is_file() or set(path.parts) & _SKIPPED_DIRECTORIES:
                continue
            if path.suffix in {".py", ".sh", ".go", ".yaml", ".yml", ".template", ".json", ".ts", ".tsx"}:
                chunks.append(path.read_text(errors="ignore"))
    return "\n".join(chunks)


def _roots_for(compose_name, service):
    key = (compose_name, service)
    if key in PLAIN_SERVICES:
        return PLAIN_SERVICES[key]
    role = ROLE_SERVICES[key]
    closure = _dependency_closure(ROLE_DISTRIBUTIONS[role])
    assert closure, f"could not resolve the dependency closure of {ROLE_DISTRIBUTIONS[role]!r}"
    return [PACKAGES / dist / "src" for dist in sorted(closure)]


CHECKED = sorted(ROLE_SERVICES) + sorted(PLAIN_SERVICES)


@pytest.mark.parametrize("compose_name,service", CHECKED, ids=lambda value: value.replace("docker-compose.", ""))
def test_every_variable_handed_to_a_container_is_read_inside_it(compose_name, service):
    blocks = _service_blocks(compose_name)
    assert service in blocks, f"{compose_name} no longer declares a {service!r} service — this guard is stale."

    wanted = _environment_variables(blocks[service])
    assert wanted, (
        f"{compose_name}:{service} passes no ${{VAR}} into the container at all — either the file changed "
        f"shape or this guard's parser stopped matching, and it would pass vacuously either way."
    )

    sources = _source_text(_roots_for(compose_name, service))
    assert sources, f"read no source for {compose_name}:{service} — the guard would pass vacuously."

    unread = sorted(name for name in wanted if name not in sources)
    assert unread == [], f"{compose_name}:{service} is handed {unread}, which nothing in its image reads.\n\n{WHY}"


def test_the_scan_reads_the_bot_image_and_not_its_test_suite():
    """§87: excluding tests must not blind the guard to the code that ships.

    Two halves, and the second is why the exclusion is safe. `SHARED_REDIS_URI` is read by
    `tg_bot/bot_events_consumer.py` -- working code, still visible. `RATELIMIT_STORAGE_URI` appears
    in `tg_bot/` exactly once, in a test asserting the consumer ignores it, and must now be invisible;
    while it was visible, this guard reported that the Telegram poller reads a variable it has never
    heard of, and `docker-compose.bot.yml` went on refusing to start without a value for it.
    """

    sources = _source_text(_roots_for("docker-compose.bot.yml", "bot"))

    assert "SHARED_REDIS_URI" in sources, (
        "the exclusion swallowed working code — tg_bot/bot_events_consumer.py reads this and the guard "
        "can no longer see it, which makes every remaining assertion vacuous."
    )
    assert "BACKEND_API_URL" in sources, "the exclusion swallowed tg_bot/config.py"
    assert "RATELIMIT_STORAGE_URI" not in sources, (
        "the poller's image still appears to read RATELIMIT_STORAGE_URI. If a real reader was added, "
        "put the variable back in docker-compose.bot.yml; if this is tg_bot/tests again, the exclusion "
        "in _SKIPPED_DIRECTORIES stopped matching."
    )


def test_the_distribution_closure_is_really_role_specific():
    """Mutation insurance: if the closure ever resolved to "every package", the guard above says nothing."""

    botapi = _dependency_closure("panel-botapi")
    assert "panel-adminapi" not in botapi, (
        "panel-botapi now depends on panel-adminapi. That is not necessarily wrong, but the PANEL_SECRET_PATH "
        "finding this guard exists for turned on that edge being absent — recheck the bot host's variables."
    )
    assert "panel-core" in botapi, "the closure walker stopped following dependencies"
    assert _dependency_closure("panel-master") != botapi, "every role resolved to the same closure"
