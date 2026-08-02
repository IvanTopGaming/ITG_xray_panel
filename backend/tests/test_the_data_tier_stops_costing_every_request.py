"""§112 and §115: what talking to a Redis that is not there costs.

Two defects with one shape — a timeout that nobody remembered. The subscriber relied on redis-py's
old "block forever" default, which version 8 changed to five seconds, so a channel that is quiet
(which `panel:refresh` almost always is) looked like a dropped connection: the listener logged,
slept five seconds and resubscribed, leaving the channel with no reader for about half of every
cycle. Measured on a live stand, 5 of 12 nudges produced an out-of-band poll and 7 were lost.

The request-path clients had the opposite problem. One-second timeouts are right for a single call
and wrong for an outage, because nothing remembered the failure: every request re-tried every
lookup from scratch, so a subscription answer that costs 0.5 s healthy cost 4-10 s while the data
tier was unreachable, and the master's own pages 2.7-3.5 s.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from panel_core import extensions


@pytest.fixture(autouse=True)
def _clean_clients():
    extensions.reset_redis_clients()
    yield
    extensions.reset_redis_clients()


def test_the_subscriber_never_times_out_on_a_quiet_channel(monkeypatch):
    monkeypatch.setenv(extensions.SHARED_REDIS_URI_ENV, "redis://data-tier:6379/0")
    client = extensions.new_shared_redis_subscriber()
    assert client is not None

    kwargs = client.connection_pool.connection_kwargs
    assert "socket_timeout" in kwargs, (
        "socket_timeout was left to redis-py's default, which is 5 seconds since version 8. A quiet "
        "pubsub channel then raises TimeoutError, run_refresh_listener treats that as a dropped "
        "connection and sleeps before resubscribing, and panel:refresh loses roughly half its messages."
    )
    assert kwargs["socket_timeout"] is None, (
        f"the blocking subscriber must wait indefinitely; it was given {kwargs['socket_timeout']!r}"
    )

    from redis.connection import Connection

    assert Connection(**kwargs).socket_timeout is None, (
        "the connection redis-py actually builds still carries a read timeout, so the reconnect loop "
        "is back even though connection_kwargs looks right"
    )


def test_a_failed_call_is_not_retried_by_the_next_request(monkeypatch):
    monkeypatch.setenv(extensions.LOCAL_REDIS_URI_ENV, "redis://local-box:6379/0")

    slow = MagicMock()
    slow.get.side_effect = OSError("connect timed out")

    with patch("redis.Redis.from_url", return_value=slow):
        client = extensions.get_redis()

    with pytest.raises(OSError):
        client.get("anything")
    assert slow.get.call_count == 1

    for _ in range(5):
        with pytest.raises(Exception):
            client.get("anything")

    assert slow.get.call_count == 1, (
        f"every request paid the socket timeout again: the client was called {slow.get.call_count} "
        f"times for six lookups. While the data tier is unreachable each subscription request re-tries "
        f"every lookup from scratch, which is what turned a 0.5 s answer into 4-10 s."
    )


def test_the_circuit_closes_again_once_the_tier_answers(monkeypatch):
    monkeypatch.setenv(extensions.LOCAL_REDIS_URI_ENV, "redis://local-box:6379/0")

    flaky = MagicMock()
    flaky.get.side_effect = OSError("connect timed out")
    with patch("redis.Redis.from_url", return_value=flaky):
        client = extensions.get_redis()

    with pytest.raises(OSError):
        client.get("k")

    with patch.object(time, "monotonic", return_value=time.monotonic() + extensions.CIRCUIT_OPEN_SECONDS + 1):
        flaky.get.side_effect = None
        flaky.get.return_value = b"v"
        assert client.get("k") == b"v", "the client stayed open after the window expired and never recovered"

    flaky.get.side_effect = OSError("down again")
    with pytest.raises(OSError):
        client.get("k")
    with pytest.raises(Exception):
        client.get("k")


def test_a_healthy_tier_is_not_slowed_down_by_the_breaker(monkeypatch):
    monkeypatch.setenv(extensions.SHARED_REDIS_URI_ENV, "redis://data-tier:6379/0")

    healthy = MagicMock()
    healthy.get.return_value = b"ok"
    with patch("redis.Redis.from_url", return_value=healthy):
        client = extensions.get_shared_redis()

    for _ in range(10):
        assert client.get("k") == b"ok"
    assert healthy.get.call_count == 10, "the breaker swallowed calls while the tier was answering"
