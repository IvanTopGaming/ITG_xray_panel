import ast
from unittest.mock import MagicMock, patch

import pytest

from tests.import_graph import source_path

LOCAL_URI = "redis://local-box:6379/0"
SHARED_URI = "redis://data-tier:6379/0"

WHY = (
    "Wave 2 split the two Redis instances by WHO NEEDS THE DATA rather than who asked first. The local "
    "Redis on the master and on a node now carries rate limits and that role's own subscription cache — "
    "nothing else. Everything more than one role needs lives in the data tier: the bot:events bus, the "
    "node snapshots and the panel:refresh nudge.\n\n"
    "get_redis() reads RATELIMIT_STORAGE_URI, which resolves to a DIFFERENT server on the master and on "
    "every node than it does on sub and bot-api. A shared-tier value read through it therefore lands in a "
    "per-host Redis that the roles which need it cannot see, and nothing anywhere reports an error: the "
    "write succeeds, the read returns empty, and the reader treats empty as 'there is nothing'. That is "
    "exactly how the node snapshots came to be written by the master into its own Redis while sub looked "
    "for them in the data tier — the subscription silently shrank to local servers a minute after every "
    "purchase.\n\n"
    "Fixing only the cron job would have left the split alive: thirteen proxy_* operations on the master "
    "write the same keys."
)

SHARED_TIER_MODULES = {
    "services/panel_proxy.py": (
        "node snapshots, the status/last_poll markers and the panel:refresh nudge",
        {"get_shared_redis"},
    ),
    "jobs/panels.py": (
        "the poller that is now the single writer of those snapshots",
        {"store_panel_snapshot", "store_panel_offline", "new_shared_redis_subscriber"},
    ),
    "api/panels.py": (
        "the Panels page overlay and the key removal when a panel is deleted",
        {"get_panel_liveness", "forget_panel"},
    ),
    "services/bot_events.py": ("the bot:events bus", {"get_shared_redis"}),
    "services/bot_status.py": (
        "the bot's reported version, written on bot-api and read on the master (§67)",
        {"get_shared_redis"},
    ),
}


def _referenced_names(relative):
    tree = ast.parse(source_path(relative).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


@pytest.mark.parametrize("relative", sorted(SHARED_TIER_MODULES))
def test_shared_tier_modules_never_reach_for_the_local_redis(relative):
    handles, expected = SHARED_TIER_MODULES[relative]
    names = _referenced_names(relative)
    assert "get_redis" not in names, (
        f"{relative} references get_redis(), the LOCAL client, although it handles {handles} — data other "
        f"roles have to see. Use get_shared_redis(), or one of the panel_proxy helpers built on it.\n\n{WHY}"
    )
    assert names & expected, (
        f"{relative} references none of {sorted(expected)}, so the assertion above would pass vacuously if "
        f"the module simply stopped handling {handles}.\n\n{WHY}"
    )


def test_only_the_limiter_and_the_startup_check_read_the_rate_limit_uri():

    from tests.import_graph import SRC_ROOTS

    allowed = {"extensions.py", "app_base.py", "sub_cache.py"}
    offenders = []
    for root in SRC_ROOTS:
        for path in root.rglob("*.py"):
            if path.name in allowed:
                continue
            if "RATELIMIT_STORAGE_URI" in path.read_text():
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], (
        f"{sorted(offenders)} read RATELIMIT_STORAGE_URI directly. After the split that variable names one "
        f"thing only — where THIS role's rate limits and its own subscription cache live. Reading it for "
        f"anything else re-creates the ambiguity the rename was for.\n\n{WHY}"
    )


def _redis_calls(monkeypatch, action):
    from panel_core.extensions import reset_redis_clients

    monkeypatch.setenv("RATELIMIT_STORAGE_URI", LOCAL_URI)
    monkeypatch.setenv("SHARED_REDIS_URI", SHARED_URI)
    reset_redis_clients()

    built = {}

    def fake_from_url(uri, **kwargs):
        client = MagicMock()
        client.get.return_value = None
        built[uri] = client
        return client

    with patch("redis.Redis.from_url", side_effect=fake_from_url):
        action()
    reset_redis_clients()
    return built


def test_the_node_snapshot_is_read_from_the_shared_tier(monkeypatch):
    from panel_core.services.panel_proxy import get_panel_snapshot

    built = _redis_calls(monkeypatch, lambda: get_panel_snapshot(7))

    assert LOCAL_URI not in built, f"the snapshot was read from the local Redis\n\n{WHY}"
    assert built[SHARED_URI].get.call_args.args[0] == "panel:7:snapshot"


def test_the_node_snapshot_is_written_to_the_shared_tier(monkeypatch):
    from panel_core.services.panel_proxy import store_panel_snapshot

    built = _redis_calls(monkeypatch, lambda: store_panel_snapshot(7, {"inbounds": []}, 1781200000000))

    assert LOCAL_URI not in built, f"the snapshot was written to the local Redis\n\n{WHY}"
    keys = [call.args[0] for call in built[SHARED_URI].setex.call_args_list]
    assert keys == ["panel:7:snapshot", "panel:7:status", "panel:7:last_poll"]


def test_the_refresh_nudge_goes_to_the_shared_tier(monkeypatch):
    from panel_core.services.panel_proxy import REFRESH_CHANNEL, _nudge_panel_refresh

    built = _redis_calls(monkeypatch, lambda: _nudge_panel_refresh(7))

    assert LOCAL_URI not in built, f"the nudge was published into the local Redis\n\n{WHY}"
    built[SHARED_URI].publish.assert_called_once_with(REFRESH_CHANNEL, "7")


def test_the_subscription_cache_stays_local_but_its_invalidation_reaches_the_shared_tier(monkeypatch, app):

    from panel_core.services import sub_cache

    read = _redis_calls(monkeypatch, lambda: sub_cache.get("v2ray", "uuid-1"))
    assert SHARED_URI not in read, (
        "the subscription cache was READ from the shared tier. Each role caches the response IT builds, and "
        "a node builds it from its own SQLite while sub builds it from Postgres — the same key would hold "
        "two different answers. On top of that a node's data-tier credential is publish-only, so this would "
        f"simply stop working there.\n\n{WHY}"
    )
    assert read[LOCAL_URI].get.call_args.args[0] == "sub:v2ray:uuid-1"

    with app.app_context():
        invalidated = _redis_calls(monkeypatch, lambda: sub_cache.invalidate_user("uuid-1"))
    assert SHARED_URI in invalidated, (
        "invalidation did not reach the shared tier, so the sub host's cached copy survives an admin edit "
        "for up to SUB_CACHE_TTL_SECONDS. This is the one half of the subscription cache that more than one "
        f"role needs, and the whole of finding 28.\n\n{WHY}"
    )
    assert LOCAL_URI in invalidated, (
        "invalidation stopped clearing the role's OWN cache. The role that just changed a user serves the "
        f"stale answer itself until the TTL runs out.\n\n{WHY}"
    )
    for uri in (LOCAL_URI, SHARED_URI):
        assert invalidated[uri].delete.call_args.args == (
            "sub:v2ray:uuid-1",
            "sub:clash:uuid-1",
            "sub:singbox:uuid-1",
        )


def test_one_instance_is_deleted_from_once_when_both_uris_are_the_same(monkeypatch, app):

    from panel_core.extensions import reset_redis_clients
    from panel_core.services import sub_cache

    monkeypatch.setenv("RATELIMIT_STORAGE_URI", SHARED_URI)
    monkeypatch.setenv("SHARED_REDIS_URI", SHARED_URI)
    reset_redis_clients()

    clients = []

    def fake_from_url(uri, **kwargs):
        client = MagicMock()
        clients.append(client)
        return client

    with patch("redis.Redis.from_url", side_effect=fake_from_url), app.app_context():
        sub_cache.invalidate_user("uuid-2")
    reset_redis_clients()

    deletes = sum(client.delete.call_count for client in clients)
    assert deletes == 1, (
        "sub and bot-api point both variables at the same server; the invalidation must not fan out to it "
        "twice just because it is named twice."
    )
