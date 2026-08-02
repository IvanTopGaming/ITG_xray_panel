"""§65 and §56: a rate limiter that cannot reach its counters must not refuse the request.

`Limiter` was built with neither `swallow_errors` nor a fallback, so an unreachable storage backend
raises out of the decorator and Flask answers **HTTP 500**. That is a defensible default when the
storage is a container in the same compose stack. It is not defensible on the two hosts whose
`RATELIMIT_STORAGE_URI` points at *another machine*:

    sub      all four subscription routes carry a limit -> a dead data-tier Redis means every
             client app gets 500 instead of its configuration. Nothing else on that path fails
             this way: `sub_cache` catches the same exception and falls through past the cache.
    bot-api  one route, the YooKassa webhook. Smaller blast radius and it has a designed fallback
             (`poll_pending_payments`, 30 s), which is why the sub host is the reason this exists.

§56 recorded the same mechanism on a node's `POST /api/federation/handshake` -- the only
unauthenticated route it serves -- and called the scenario rare. It is not rare: a node whose own
Redis has not finished starting is exactly the state a node is in when it is first brought up, and
handshake is the first thing an admin does to it.

**What this wave did NOT choose, and why the distinction is the whole point.** `swallow_errors=True`
would also have removed the 500 -- by removing the limit. Every limit, on every role, including
`10 per minute` on the admin login. `RATELIMIT_IN_MEMORY_FALLBACK_ENABLED` instead moves the
counters into the process for as long as the storage is unreachable, and moves them back on its own
when it returns. Since every gunicorn command in this repo runs `-w 1`, one process is one host, so
the degraded counter covers exactly the same population as the Redis-backed one.

The assertions therefore come in pairs. "Answers 200 with a dead storage" alone is also what
`swallow_errors` looks like, and what deleting the decorator looks like. Every one of those is
paired with "and the limit still fires".
"""

from __future__ import annotations

import importlib
import socket

import pytest

from panel_core.extensions import db, limiter, scheduler
from panel_core.models import Client, Inbound, TelegramUser

from tests.schema import ensure_schema


SUB_TOKEN = "sub-token-served-while-the-data-tier-is-down"

ROLES = (("master", "master"), ("worker", "worker"), ("sub", "sub"), ("botapi", "bot"), ("cron", "cron"))


def _closed_port() -> int:
    """A port with nothing listening. `redis://127.0.0.1:<port>` refuses instantly on loopback.

    Мock-based tests prove nothing here: the property is about what a *real* storage error does to
    a *real* request, and a patched client is neither.
    """

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


DEAD_STORAGE = f"redis://127.0.0.1:{_closed_port()}/0"


@pytest.fixture(autouse=True)
def _limiter_teardown():
    """The limiter is a module-level singleton, so a dead-storage test would leak into the next one."""

    yield
    limiter._storage_dead = False
    limiter._Limiter__check_backend_count = 0
    limiter._Limiter__last_check_backend = 0
    try:
        limiter.reset()
    except Exception:
        pass
    if scheduler.running:
        scheduler.shutdown(wait=False)
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


def _build(monkeypatch, tmp_path, module, storage_uri, *, role=None, with_database_url=True):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", role or module)
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", storage_uri)
    monkeypatch.chdir(tmp_path)
    gw.set_xray_gateway(None)
    if with_database_url:
        monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/{module}.db"))
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    app = importlib.import_module(f"panel_core.roles.{module}").create_app()
    try:
        limiter.reset()
    except Exception:
        pass
    return app


@pytest.mark.parametrize("module,role", ROLES, ids=[module for module, _ in ROLES])
def test_every_role_declares_the_in_memory_fallback(monkeypatch, tmp_path, module, role):
    """The flag lives on the app, and `limiter.init_app` reads it once per app.

    Asserted per role rather than once on `build_base_app`, because the five roles reach it through
    five factories and a role that stopped calling the shared builder would lose it silently.
    """

    app = _build(monkeypatch, tmp_path, module, "memory://", role=role)

    assert app.config.get("RATELIMIT_IN_MEMORY_FALLBACK_ENABLED") is True, (
        f"role {role!r} builds an app whose limiter has no fallback: an unreachable "
        f"RATELIMIT_STORAGE_URI would make every limited route on it answer HTTP 500."
    )
    assert limiter._in_memory_fallback_enabled, "flask-limiter did not pick the config up at init_app time"


def test_the_subscription_survives_a_dead_rate_limit_store(monkeypatch, tmp_path):
    """§65, the headline: the data tier's Redis is down and a client app still gets its config."""

    app = _build(monkeypatch, tmp_path, "sub", DEAD_STORAGE)

    with app.app_context():
        db.session.add(Inbound(tag="vless-reality", port=443, protocol="vless", stream_settings="{}"))
        db.session.add(TelegramUser(telegram_id=700, sub_token=SUB_TOKEN, language="ru"))
        db.session.add(
            Client(
                id="11111111-2222-3333-4444-555555555555",
                email="tg700_vless",
                inbound_tag="vless-reality",
                telegram_id=700,
                enable=True,
            )
        )
        db.session.commit()

    client = app.test_client()
    response = client.get(f"/api/sub/u/{SUB_TOKEN}", headers={"User-Agent": "v2rayNG/1.8"})

    assert response.status_code == 200, (
        "the sub role answered "
        f"{response.status_code} with an unreachable RATELIMIT_STORAGE_URI. This is the finding: the "
        "limiter is the only thing on the config path that turns a neighbour's outage into a total "
        "refusal, and users stop receiving keys."
    )
    assert response.data, "served an empty body — the handler was reached but produced nothing"


def test_the_node_can_still_be_linked_while_its_own_redis_is_down(monkeypatch, tmp_path):
    """§56: handshake is the one unauthenticated route a node serves, and the first one an admin uses."""

    app = _build(monkeypatch, tmp_path, "worker", DEAD_STORAGE, with_database_url=False)

    response = app.test_client().post("/api/federation/handshake", json={"link_token": "nonsense"})

    assert response.status_code != 500, (
        "a node whose Redis has not finished starting answers 500 to a link attempt, which is exactly "
        "the state it is in when first brought up."
    )
    assert response.status_code == 401, f"expected the node's own refusal of a bogus token, got {response.status_code}"


def test_a_dead_store_degrades_the_limit_rather_than_removing_it(monkeypatch, tmp_path):
    """The half that separates this from `swallow_errors`, and from deleting the decorator.

    30 per minute on handshake, counted in the process while the storage is unreachable.
    """

    app = _build(monkeypatch, tmp_path, "worker", DEAD_STORAGE, with_database_url=False)
    client = app.test_client()

    codes = [client.post("/api/federation/handshake", json={"link_token": "x"}).status_code for _ in range(31)]

    assert codes[0] != 500, "the dead storage refused the very first request"
    assert codes.count(429) == 1 and codes[-1] == 429, (
        "with the storage unreachable the limit stopped being enforced at all — that is fail-open, not "
        f"degradation, and it applies to the admin login too. Codes: {sorted(set(codes))}"
    )


def test_a_working_store_still_enforces_the_limit(monkeypatch, tmp_path):
    """The other end. Without this, "survives a dead store" is also what a removed limit looks like."""

    app = _build(monkeypatch, tmp_path, "worker", "memory://", with_database_url=False)
    client = app.test_client()

    codes = [client.post("/api/federation/handshake", json={"link_token": "x"}).status_code for _ in range(31)]

    assert limiter._storage_dead is False, "the storage was reachable, yet the limiter fell back anyway"
    assert codes.count(429) == 1 and codes[-1] == 429, (
        f"the 30/minute limit on handshake no longer fires against a working storage: {sorted(set(codes))}"
    )
