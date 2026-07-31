"""§61: a federated call that fails must arrive as the node's own words, not as a status line.

`FederationClient` had two helpers doing the same job with different manners. `_call_reporting`
turns a failure into `RemotePanelError` carrying the node's message and its status code, which is
what lets a handler answer 502 "Panel is unreachable: …" or relay "Rule #1 has unknown outbound
target: …" verbatim. `_call` just let `requests` raise, and `requests.HTTPError` stringifies to a
status line, so every one of those handlers fell through to its generic branch and the admin read
"Remote panel error" while the node had said something specific.

Waves 4c-2, 4d and 5c wrote their twenty-four methods against the reporting helper. The fourteen
that predate them did not move: `snapshot`, inbound CRUD, user CRUD, the six batch operations,
`reset_traffic` and `provision`. This closes that, and `_call` is gone rather than left beside its
replacement -- two helpers where one is used is exactly how the split lasted three waves.

**Why every method, not a sample (§80).** The property is held by one word per method, and methods
are added one at a time. This file therefore enumerates `FederationClient` by reflection and builds
each call's arguments from its own signature, so a method added tomorrow is covered the day it is
written rather than the day someone remembers to list it.

**`provision` is in the list on purpose.** It is the money path, and the change was checked against
every caller before being made: nothing anywhere catches a `requests` exception type -- `apply_payment`
and the six batch handlers all catch bare `Exception` -- so the control flow is identical and only
the message the admin (or the `errors[]` field) ends up holding changes. The batch handlers are the
visible win: they stringify whatever they catch straight into the response.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest
import requests

from panel_core.services.panel_proxy import FederationClient, RemotePanelError


NODE_MESSAGE = "Rule #1 has unknown outbound target: amsterdam-egress"

VERBS = ("get", "post", "put", "delete")


def _dummy(name: str):
    """Arguments built from the parameter's own name, so a new method needs no entry here."""

    if name in ("payload", "params", "user_data"):
        return {}
    if name == "users":
        return [{"tag": "t", "email": "e"}]
    if name in ("enable",):
        return True
    if name in ("days", "profile_id", "telegram_id", "amount_bytes"):
        return 1
    if name == "mode":
        return "add"
    return "x"


def _client_methods():
    client = FederationClient("https://node.example.com", "tok")
    found = []
    for name, member in inspect.getmembers(client, predicate=inspect.ismethod):
        if name.startswith("_"):
            continue
        params = list(inspect.signature(member).parameters)
        found.append((name, [_dummy(p) for p in params]))
    assert len(found) >= 30, f"only {len(found)} methods enumerated — the reflection stopped working"
    return sorted(found)


METHODS = _client_methods()
IDS = [name for name, _ in METHODS]


def _response(status_code: int, body):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    return resp


def _patched_session(client, **kwargs):
    return [patch.object(client._session, verb, **kwargs) for verb in VERBS]


@pytest.mark.parametrize("name,args", METHODS, ids=IDS)
def test_an_unreachable_node_arrives_as_502(name, args):
    client = FederationClient("https://node.example.com", "tok")
    patches = _patched_session(client, side_effect=requests.ConnectionError("no route to host"))
    for p in patches:
        p.start()
    try:
        with pytest.raises(RemotePanelError) as caught:
            getattr(client, name)(*args)
    finally:
        for p in patches:
            p.stop()

    assert caught.value.status_code == 502, (
        f"{name}() reports an unreachable node as {caught.value.status_code}, not 502 — the handler "
        f"above it cannot tell 'the node is down' from 'the node refused'."
    )
    assert "unreachable" in str(caught.value)


@pytest.mark.parametrize("name,args", METHODS, ids=IDS)
def test_the_nodes_own_message_survives(name, args):
    client = FederationClient("https://node.example.com", "tok")
    patches = _patched_session(client, return_value=_response(400, {"error": NODE_MESSAGE}))
    for p in patches:
        p.start()
    try:
        with pytest.raises(RemotePanelError) as caught:
            getattr(client, name)(*args)
    finally:
        for p in patches:
            p.stop()

    assert caught.value.status_code == 400
    assert NODE_MESSAGE in str(caught.value), (
        f"{name}() swallowed the node's own explanation. That is the whole of §61: the admin is shown "
        f"a generic error while the node was telling them exactly what was wrong."
    )


def test_the_plain_call_helper_is_gone():
    """Mutation insurance: leaving it beside its replacement is how the split survived three waves."""

    assert not hasattr(FederationClient, "_call"), (
        "FederationClient._call is back. Every method reports through _call_reporting now; a second "
        "helper that raises requests' own exceptions only exists to be picked by mistake."
    )
    assert hasattr(FederationClient, "_call_reporting")
