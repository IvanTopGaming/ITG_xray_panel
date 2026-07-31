"""П2 / §7.2: the node snapshot expires 60 seconds after the poller stops, and the user is then told
his subscription does not exist.

The master keeps no `Client` row for a node-issued client -- `Client` has no `panel_id` and nothing
mirrors them -- so in a split topology the *only* record of what a node serves is
`panel:<id>:snapshot` in the data-tier Redis, written every 10 s by the cron host and given a 60 s
TTL. Kill the cron host (or the network to it) and one minute later:

    get_panel_snapshot() -> None            for every reader
    the sub role builds the config out of its own Client rows, of which there are none
    get_subscription_content_for_user() -> []
    HTTP 404 "User not found"

A client app is not told "temporarily unavailable"; it is told *this subscription does not exist*.
The bot's "Keys" and "Statistics" screens empty out through the same door.

**The shape of the fix, and the two halves that must not be separated.** Every write now lands
twice: the live key with its TTL, and `panel:<id>:snapshot:last` with none. A read prefers the live
one and falls back to the last known copy. That converts an unbounded outage of the poller from
"users lose their VPN" into "users keep the configuration they had".

The price is real and is accepted deliberately: **a client disabled or deleted while the pipeline is
down goes on being served** until it comes back. The deployment already carries two windows of
exactly this kind -- the 60 s subscription cache and the 24 h `profile-update-interval` announced to
client apps -- so this is a third of a familiar shape, not a new class.

Because of that price the wave is only half done if the fallback is silent: it would replace a loud
failure with a quiet lie, which is the class waves 5a-5c spent three rounds removing. So the second
half is mandatory and asserted here too -- a WARNING on the serving role, and `status: "stale"` with
the age of the copy on the master's Panels card, where an admin can act on it.

**What must NOT get the stale copy.** `fetch_panel_snapshot_live` exists because a stale cache must
never let a block or a revoke silently no-op. Those paths make an HTTP call to the node every time
and are asserted below to stay that way: there, out-of-date data means "we failed to cut off someone
we were told to cut off", which is a defect and not resilience.
"""

from __future__ import annotations

import importlib
import json
import logging

import pytest

from panel_core.services import panel_proxy

from tests.schema import ensure_schema


PANEL_ID = 7
LAST_POLL_MS = 1781200000000

NODE_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SUB_TOKEN = "sub-token-that-outlives-the-poller"

SNAPSHOT = {
    "inbounds": [
        {
            "tag": "DE-vless",
            "port": 8443,
            "protocol": "vless",
            "label": "Germany",
            "stream_settings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "publicKey": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "shortIds": ["abcd1234"],
                    "fingerprint": "chrome",
                    "serverNames": ["google.com"],
                },
            },
            "clients": [
                {
                    "id": NODE_UUID,
                    "email": "tg700_DE-vless",
                    "enable": True,
                    "up": 111,
                    "down": 222,
                    "limit_bytes": 3000,
                    "expiry_time": 1800000000000,
                    "telegram_id": 700,
                }
            ],
        }
    ]
}


class FakeRedis:
    """Records which writes carry a TTL, because that is the whole of the mechanism.

    `expire_volatile()` is what the data tier does on its own once the poller stops: everything
    written with `setex` disappears, everything written with `set` stays. A `set` quietly changed
    back to `setex` therefore fails these tests rather than passing them.
    """

    def __init__(self):
        self.values: dict[str, bytes] = {}
        self.volatile: set[str] = set()

    @staticmethod
    def _encode(value):
        return value if isinstance(value, bytes) else str(value).encode()

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = self._encode(value)
        self.volatile.discard(key)

    def setex(self, key, ttl, value):
        assert ttl > 0
        self.values[key] = self._encode(value)
        self.volatile.add(key)

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.volatile.discard(key)

    def expire_volatile(self):
        for key in list(self.volatile):
            self.values.pop(key, None)
        self.volatile.clear()


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(panel_proxy, "get_shared_redis", lambda: fake)
    panel_proxy._stale_warned.clear()
    yield fake
    panel_proxy._stale_warned.clear()


def _stored(redis):
    panel_proxy.store_panel_snapshot(PANEL_ID, SNAPSHOT, LAST_POLL_MS)
    return redis


def test_a_poll_writes_the_last_known_copy_without_a_ttl(redis):
    _stored(redis)

    assert set(redis.values) == {
        f"panel:{PANEL_ID}:snapshot",
        f"panel:{PANEL_ID}:status",
        f"panel:{PANEL_ID}:last_poll",
        f"panel:{PANEL_ID}:snapshot:last",
        f"panel:{PANEL_ID}:last_poll:last",
    }
    assert redis.volatile == {
        f"panel:{PANEL_ID}:snapshot",
        f"panel:{PANEL_ID}:status",
        f"panel:{PANEL_ID}:last_poll",
    }, (
        "the last-known copy was written with a TTL, which makes it expire alongside the live key and "
        "leaves the outage window exactly where it was."
    )
    assert json.loads(redis.values[f"panel:{PANEL_ID}:snapshot:last"]) == SNAPSHOT
    assert redis.values[f"panel:{PANEL_ID}:last_poll:last"] == str(LAST_POLL_MS).encode()


def test_a_fresh_snapshot_is_preferred_and_says_nothing(redis, caplog):
    _stored(redis)

    with caplog.at_level(logging.WARNING):
        assert panel_proxy.get_panel_snapshot(PANEL_ID) == SNAPSHOT

    assert panel_proxy.get_panel_liveness(PANEL_ID) == ("online", LAST_POLL_MS)
    assert not [r for r in caplog.records if "stale" in r.message or "last known" in r.message], (
        "a healthy deployment must not log a degradation on every read"
    )


def test_the_last_known_copy_answers_once_the_poller_stops(redis, caplog):
    _stored(redis)
    redis.expire_volatile()

    with caplog.at_level(logging.WARNING):
        served = panel_proxy.get_panel_snapshot(PANEL_ID)

    assert served == SNAPSHOT, (
        "the cron host has been down for over a minute and the snapshot is gone. Before this wave the "
        "reader got None here, and the user was told his subscription does not exist."
    )
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, (
        "served an out-of-date copy and said nothing. That trades a loud failure for a quiet lie: a "
        "client disabled since the last poll is still being served, and nobody can find out."
    )
    assert str(LAST_POLL_MS) in warnings[0].getMessage(), "the log line does not say how old the copy is"


def test_the_warning_is_not_repeated_on_every_request(redis, caplog):
    _stored(redis)
    redis.expire_volatile()

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            panel_proxy.get_panel_snapshot(PANEL_ID)

    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1, (
        "the sub host answers every config request through this function; one line per request would "
        "bury the node's log and everything worth reading in it."
    )


def test_a_recovered_poller_clears_the_warning_state(redis, caplog):
    _stored(redis)
    redis.expire_volatile()
    panel_proxy.get_panel_snapshot(PANEL_ID)

    _stored(redis)
    assert panel_proxy.get_panel_snapshot(PANEL_ID) == SNAPSHOT
    redis.expire_volatile()

    # caplog accumulates across the whole test, so the first outage's line would satisfy the
    # assertion below on its own and the mutation "never clear the suppression" stayed green.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        panel_proxy.get_panel_snapshot(PANEL_ID)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "a second outage after a recovery went unreported — the suppression is permanent rather than per-outage."
    )


def test_a_panel_that_was_never_polled_still_reads_as_nothing(redis):
    assert panel_proxy.get_panel_snapshot(PANEL_ID) is None
    assert panel_proxy.get_panel_liveness(PANEL_ID) == (None, None)


def test_the_admin_is_told_the_panel_is_no_longer_being_polled(redis):
    """The visible half. Without it the wave buys resilience with silence.

    `list_panels` overlays whatever this returns onto the DB row. When the live status key expired
    it used to return `(None, None)`, so the page fell back to `LinkedPanel.status` -- last written
    by the cron host on a *change* -- and showed a node that nothing had contacted for hours as
    `online`, with a `Last Poll` that also came from the same stale row.
    """

    _stored(redis)
    redis.expire_volatile()

    assert panel_proxy.get_panel_liveness(PANEL_ID) == ("stale", LAST_POLL_MS)
    assert panel_proxy.STALE_STATUS == "stale"


def test_a_node_offline_while_the_poller_lives_is_still_reported_offline(redis):
    """`stale` must mean "nobody is polling", never "the node is down" — the admin acts differently."""

    _stored(redis)
    panel_proxy.store_panel_offline(PANEL_ID)

    status, _ = panel_proxy.get_panel_liveness(PANEL_ID)
    assert status == "offline", f"a live poller reporting a dead node came out as {status!r}"


def test_deleting_a_panel_removes_the_immortal_keys_too(redis):
    _stored(redis)
    panel_proxy.forget_panel(PANEL_ID)

    assert redis.values == {}, (
        "the two keys with no TTL survived the panel being deleted. They would never expire, and a "
        "panel re-added under the same id would be served a stranger's inbounds."
    )


def test_the_subscription_still_carries_the_node_after_the_poller_dies(monkeypatch, tmp_path, redis):
    """End to end on the sub role, stubbing nothing above the Redis client itself."""

    from panel_core.extensions import db
    from panel_core.models import LinkedPanel, TelegramUser
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "sub")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/sub.db"))
    monkeypatch.chdir(tmp_path)
    gw.set_xray_gateway(None)
    app = importlib.import_module("panel_core.roles.sub").create_app()

    with app.app_context():
        panel = LinkedPanel(
            name="de", url="https://node1.example.com", federation_token="tok", enable=True, created_at=0
        )
        db.session.add(panel)
        db.session.add(TelegramUser(telegram_id=700, sub_token=SUB_TOKEN, language="ru"))
        db.session.commit()
        panel_id = panel.id

    monkeypatch.setattr("panel_core.services.sub_cache.get", lambda kind, key: None)
    monkeypatch.setattr("panel_core.services.sub_cache.set", lambda kind, key, value: None)

    panel_proxy.store_panel_snapshot(panel_id, SNAPSHOT, LAST_POLL_MS)
    redis.expire_volatile()

    response = app.test_client().get(f"/api/sub/u/{SUB_TOKEN}", headers={"User-Agent": "v2rayNG/1.8"})

    assert response.status_code == 200, (
        f"the cron host has been down for a minute and the sub role answered {response.status_code}. "
        "404 here is what a user's client app reads as 'this subscription does not exist'."
    )

    import base64

    assert NODE_UUID in base64.b64decode(response.data).decode(), (
        "answered 200 with a config that no longer contains the node's server — the user keeps a "
        "subscription with nothing in it, which is the same outage wearing a different status code."
    )


def test_the_destructive_paths_still_go_to_the_node_itself():
    """Trap: blocking and revoking must never read a cache, stale or fresh (§ 'Destructive user ops')."""

    from panel_core.services import remote_clients

    source = importlib.import_module("panel_core.services.remote_clients").__file__
    text = open(source).read()

    assert "fetch_panel_snapshot_live" in text
    assert "get_panel_snapshot" not in text.replace("fetch_panel_snapshot_live", ""), (
        "remote_clients.py reaches the cached snapshot. Block/unblock/revoke enumerate a user's remote "
        "clients through it, and a stale answer there means failing to cut off someone an admin just "
        "cut off — the opposite of what this wave is for."
    )
    assert hasattr(remote_clients, "remote_clients_by_telegram_id_live")
