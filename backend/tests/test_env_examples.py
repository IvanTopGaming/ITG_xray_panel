import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

HOST_EXAMPLES = {
    "docker-compose.master.yml": ".env.master.example",
    "docker-compose.node.yml": ".env.node.example",
    "docker-compose.sub.yml": ".env.sub.example",
    "docker-compose.bot.yml": ".env.bot.example",
    "docker-compose.cron.yml": ".env.cron.example",
    "docker-compose.postgres.yml": ".env.data.example",
}

REQUIRED_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?")
ANY_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")

WHY = (
    "A `${VAR:?…}` in a compose file is a hard start-up requirement: without a value the whole stack "
    "refuses to come up. The example file for that host is the only place a deployer learns the variable "
    "exists. Nothing else in the repo connects the two, and the drift is one-directional and quiet — the "
    "compose file gets the new variable in the same commit that needs it, while the example is a separate "
    "edit that is easy to forget. Both BOT_EVENTS_REDIS_URI and the BACKEND_IMAGE -> four *_IMAGE split "
    "needed exactly that manual sync and nearly lost it."
)

SPLIT_WHY = (
    "The five per-host example files replaced the single shared .env.example, which could not be correct "
    "even in principle: RATELIMIT_STORAGE_URI must point at the box's OWN Redis on the master and on a "
    "node, and at the data tier on the sub and bot hosts — two mutually exclusive values of one variable, "
    "which the old file carried at once (one live, one commented out) and expected the deployer to "
    "reconcile by hand. Each file now holds only what its host reads, with no commented alternatives, "
    "which is also what makes the host-ingress narrowing unreachable to undo by config alone."
)


def _read(relative):
    path = REPO_ROOT / relative
    assert path.is_file(), f"{relative} does not exist under {REPO_ROOT}\n\n{SPLIT_WHY}"
    return path.read_text()


def _example_keys(relative):
    keys = {}
    for number, line in enumerate(_read(relative).splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys[stripped.split("=", 1)[0].strip()] = number
    assert keys, f"parsed no assignments out of {relative} — every assertion below would pass vacuously."
    return keys


@pytest.mark.parametrize("compose", sorted(HOST_EXAMPLES))
def test_every_required_variable_is_defined_in_that_hosts_example(compose):
    example = HOST_EXAMPLES[compose]
    required = set(REQUIRED_REF.findall(_read(compose)))
    assert required, (
        f"{compose} demands no ${{VAR:?…}} at all — either the file changed shape or this guard's regex "
        f"stopped matching, and it would pass vacuously either way.\n\n{WHY}"
    )
    missing = sorted(required - set(_example_keys(example)))
    assert missing == [], (
        f"{compose} refuses to start without {missing}, but {example} never mentions them. A deployer "
        f"copying that file gets a stack that dies on `up` with a bare compose error.\n\n{WHY}"
    )


@pytest.mark.parametrize("compose", sorted(HOST_EXAMPLES))
def test_no_example_defines_a_variable_its_own_compose_never_reads(compose):
    example = HOST_EXAMPLES[compose]
    referenced = set(ANY_REF.findall(_read(compose)))
    stray = sorted(set(_example_keys(example)) - referenced)
    assert stray == [], (
        f"{example} defines {stray}, which {compose} never references. Either the compose file should "
        f"pass it through explicitly, or it does not belong on this host. Both matter: a variable that "
        f"reaches a container only through `env_file` is invisible in the compose file, which is exactly "
        f"how SUB_DOMAIN came to be load-bearing for bot-api without appearing anywhere in "
        f"docker-compose.bot.yml.\n\n{SPLIT_WHY}"
    )


@pytest.mark.parametrize("compose", sorted(HOST_EXAMPLES))
def test_every_host_example_can_actually_be_committed(compose):

    import shutil
    import subprocess

    example = HOST_EXAMPLES[compose]
    _read(example)

    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git work tree")

    ignored = subprocess.run(
        [git, "check-ignore", "-q", example],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert ignored.returncode != 0, (
        f".gitignore excludes {example}, so it exists on this machine and nowhere else. The whole point of "
        f"an example file is that a deployer gets it with the repo; and every check above reads it, so on a "
        f"fresh clone they do not fail with 'this host is misconfigured' — they fail with 'file not found', "
        f"which reads like a broken test rather than a broken deploy. This is not hypothetical: the "
        f"per-host split replaced .env.example with five new files while .gitignore still said `.env.*` "
        f"with a single `!.env.example` exception, so all five were invisible to git and to CI."
    )


def test_the_shared_env_example_stays_retired():
    assert not (REPO_ROOT / ".env.example").exists(), (
        f"a shared .env.example is back at the repo root. Re-introducing it re-introduces the "
        f"contradiction it was deleted for, and invites deployers to copy one file onto every host — "
        f"which is what put every domain on every box in the first place.\n\n{SPLIT_WHY}"
    )


@pytest.mark.parametrize("variable", ["POSTGRES_BIND", "REDIS_BIND"])
def test_the_data_tier_is_not_published_to_the_world_by_default(variable):
    compose = _read("docker-compose.postgres.yml")
    default = re.search(rf"\$\{{{variable}:-([^}}]*)\}}", compose)
    assert default, f"docker-compose.postgres.yml no longer gives {variable} a `:-` default."
    assert default.group(1) != "0.0.0.0", (
        f"docker-compose.postgres.yml defaults {variable} to 0.0.0.0, i.e. every interface — the public "
        f"internet if the VM has a public IP. The data tier's Redis runs with NO TLS at all, so its ACL "
        f"password and every bot:events payload (telegram_id, client e-mails) would cross the wire in "
        f"clear. An unset variable must fail closed."
    )
    example = _read(".env.data.example")
    line = next((entry for entry in example.splitlines() if entry.startswith(f"{variable}=")), None)
    assert line, f".env.data.example does not define {variable}."
    assert line.split("=", 1)[1].strip() != "0.0.0.0", (
        f".env.data.example sets {variable}=0.0.0.0. Safety documented in a comment while the value next "
        f"to it is open is exactly the shape this was changed to stop; put this VM's private-network "
        f"address there."
    )


COMMENT_ON_AN_EMPTY_VALUE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=[ \t]*#")


@pytest.mark.parametrize("example", sorted(set(HOST_EXAMPLES.values())))
def test_no_empty_value_carries_an_inline_comment(example):
    offenders = [
        (number, line)
        for number, line in enumerate(_read(example).splitlines(), start=1)
        if COMMENT_ON_AN_EMPTY_VALUE.match(line)
    ]
    assert offenders == [], (
        f"{example} writes `KEY=` with an inline comment after it, and Docker Compose then hands "
        f"the container the COMMENT as the value. Compose strips an inline comment only when a "
        f"value precedes it: `K=v  # note` yields 'v', but `K=  # note` yields '# note'. Offending "
        f"lines: {offenders}\n\n"
        f"This shipped in 3.0.0 and stopped every TLS host from getting a certificate: ACME_EMAIL "
        f'became "# optional; where Let\'s Encrypt mails warnings" and ACME_CA became '
        f'"https://# optional; LE staging URL while rehearsing", so Caddy tried to register an '
        f"ACME account against a URL with no host and gave up on every renewal attempt. The same "
        f"shape was one line away from handing the bot its BOT_SERVICE_TOKEN as a sentence and the "
        f"data tier its Redis ACL passwords as sentences.\n\n"
        f"Put the note on its own line above the assignment instead."
    )
