"""§10.3: rotating the bot service token used to break the bot with nothing above INFO.

The panel's *Regenerate token* button writes a new value into `SystemSetting`. This process read
the old one out of its environment once, at import, and has no way to learn the new one — so from
that moment every call to bot-api answers 401. Both loops that make those calls caught everything
and logged at INFO, beside ordinary network hiccups, so a bot that had been permanently
disconnected looked exactly like a bot whose backend was briefly slow.

The panel's side of this was already honest: the confirmation modal says the bot will stop until
`BOT_SERVICE_TOKEN` is updated on the bot host. The half that was missing is here — an operator
tailing the bot's log had nothing to see.

The message says what to do rather than what happened, because the person reading it is the person
who has to fix it, and the fix is not in the panel.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from runtime_config import RuntimeConfig


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://bot-api:5000/api/bot/runtime-config")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, request=request))


@pytest.mark.parametrize("status", (401, 403))
def test_a_rejected_token_is_reported_as_an_error(caplog, status):
    cfg = RuntimeConfig()
    with caplog.at_level(logging.DEBUG, logger="runtime_config"):
        cfg._report(_http_error(status), "refreshing the runtime config")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, (
        f"HTTP {status} from bot-api was logged below ERROR. The bot is disconnected until someone "
        f"edits .env on this host, and nothing in the log says so."
    )
    message = errors[0].getMessage()
    assert "BOT_SERVICE_TOKEN" in message, "the log line does not name the variable that has to change"
    assert "restart" in message, "the log line does not say the bot has to be restarted"


def test_an_ordinary_outage_stays_at_info(caplog):
    """Otherwise a backend restart cries wolf and the real case stops standing out."""

    cfg = RuntimeConfig()
    with caplog.at_level(logging.DEBUG, logger="runtime_config"):
        cfg._report(httpx.ConnectError("connection refused"), "refreshing the runtime config")

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r for r in caplog.records if r.levelno == logging.INFO]


@pytest.mark.parametrize("status", (500, 502, 404))
def test_a_backend_fault_is_not_blamed_on_the_token(caplog, status):
    cfg = RuntimeConfig()
    with caplog.at_level(logging.DEBUG, logger="runtime_config"):
        cfg._report(_http_error(status), "refreshing the runtime config")

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_the_error_is_not_repeated_every_thirty_seconds(caplog):
    """The refresh loop runs forever; one line per cycle would bury the log it belongs in."""

    cfg = RuntimeConfig()
    with caplog.at_level(logging.DEBUG, logger="runtime_config"):
        for _ in range(5):
            cfg._report(_http_error(401), "refreshing the runtime config")

    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


def test_a_recovered_token_can_be_reported_again(caplog):
    """A second rotation after a restart-free recovery must not be swallowed by the first."""

    cfg = RuntimeConfig()
    cfg._report(_http_error(401), "refreshing the runtime config")
    cfg._token_rejected = False

    with caplog.at_level(logging.DEBUG, logger="runtime_config"):
        cfg._report(_http_error(401), "refreshing the runtime config")

    assert [r for r in caplog.records if r.levelno >= logging.ERROR]
