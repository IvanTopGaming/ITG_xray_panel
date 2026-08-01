"""§117: the shared Redis was authenticated but never encrypted.

Three ACL users with passwords, `default off`, a node that may only PUBLISH into one channel —
all of that is authorisation, and none of it is confidentiality. Read off the wire, one
`panel:<id>:snapshot` yields every client's UUID, e-mail, telegram_id, traffic and expiry, plus the
inbound's whole `stream_settings` — **including `realitySettings.privateKey`**. The first is enough
to build a working `vless://` for somebody else's account; the second is the node's *server* key,
which is enough to impersonate the node. The ACL password travels the same wire.

The deploy note offered two remedies, a private network or `rediss://`, and only the first existed:
the client accepted the scheme (`_REDIS_SCHEMES`) while `docker-compose.postgres.yml` started
`redis-server` with no `--tls-*` at all. Half a documented choice is worse than none, because it
reads as covered.

Postgres has demanded `verify-full` in exactly this position since wave 1. This is the same rule for
the same wire, enforced the same way — at start-up, loudly — with one deliberate exception: a Redis
that never leaves the machine (a compose service name, or loopback) is not a weakness, and an
all-in-one deployment reaches the shared tier that way.
"""

from __future__ import annotations

import pathlib

import pytest

from panel_core.extensions import requires_tls, validate_shared_redis_uri

REPO = pathlib.Path(__file__).resolve().parents[2]

ON_BOX = [
    "redis://redis:6379/0",
    "redis://localhost:6379/0",
    "redis://127.0.0.1:6379/0",
]

ACROSS_A_NETWORK = [
    "redis://panel:pw@data.example.com:6379/0",
    "redis://panel:pw@10.0.0.10:6379/0",
    "redis://node:pw@203.0.113.7:6379/2",
]


@pytest.mark.parametrize("uri", ON_BOX)
def test_a_redis_that_never_leaves_the_box_may_stay_plain(uri):
    assert requires_tls(uri) is False, (
        f"{uri} does not cross a network, so demanding TLS there buys nothing and would break both "
        f"the master's own rate-limit store and an all-in-one deployment"
    )
    validate_shared_redis_uri(uri, False)


@pytest.mark.parametrize("uri", ACROSS_A_NETWORK)
def test_plain_redis_to_another_machine_is_refused_at_startup(uri):
    assert requires_tls(uri) is True
    with pytest.raises(RuntimeError) as excinfo:
        validate_shared_redis_uri(uri, False)
    message = str(excinfo.value)
    assert "rediss://" in message, f"the refusal does not say what to do instead: {message!r}"
    assert "REALITY" in message, (
        f"the refusal does not say what is actually at stake, so it reads as pedantry rather than as "
        f"the node's private key crossing the internet: {message!r}"
    )


@pytest.mark.parametrize("uri", ACROSS_A_NETWORK)
def test_the_same_host_over_tls_is_accepted(uri):
    secured = uri.replace("redis://", "rediss://", 1)
    assert requires_tls(secured) is False
    validate_shared_redis_uri(secured, False)


def test_a_local_deployment_is_left_alone():
    validate_shared_redis_uri("redis://panel:pw@data.example.com:6379/0", True)


def test_the_data_tier_serves_tls_only():
    compose = (REPO / "docker-compose.postgres.yml").read_text()
    redis_block = compose[compose.index("  redis:") :]
    redis_block = redis_block[: redis_block.index("  pg-backup:")]

    assert "--tls-port 6379" in redis_block, (
        "the data tier's Redis does not speak TLS, so rediss:// on the clients cannot connect and "
        "the remedy the deploy note offers does not exist"
    )
    assert "--port 0" in redis_block, (
        "the plain port is still open beside the TLS one, so a client that forgets the scheme "
        "silently goes back to cleartext instead of failing"
    )
    assert "--tls-cert-file" in redis_block and "--tls-key-file" in redis_block


def test_every_example_reaching_the_data_tier_uses_tls():
    offenders = []
    for path in sorted(REPO.glob(".env.*.example")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            if name not in {"SHARED_REDIS_URI", "BOT_SHARED_REDIS_URI", "RATELIMIT_STORAGE_URI"}:
                continue
            if value and requires_tls(value):
                offenders.append(f"{path.name}:{number} {name}")

    assert offenders == [], (
        f"these examples hand a deployer a cleartext bus across a network: {offenders}. Every host "
        f"copies its example verbatim, so a wrong scheme here is a wrong scheme in production."
    )
