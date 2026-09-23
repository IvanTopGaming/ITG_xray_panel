"""§71: the SSRF gate on a panel URL stays shut, but it can now be opened on purpose.

`_validate_panel_url` refuses a URL whose host is a private, loopback, link-local, reserved,
multicast or unspecified address, or a bare/`.local`/`.internal`/`localhost` name. That is the right
default: `POST /api/panels` and `POST /api/panels/<id>/relink` make the master fetch whatever URL it
is handed, so without it an admin account is a request forwarder into the private network.

It also made a legitimate topology impossible. "The master and its nodes on one private segment" is
a normal way to run this product, and there was no way to express it — the check has blocked this
repo's own live stands twice, in waves 5c and 5d, both of which worked around it by writing a row
into `linked_panel` by hand.

`FEDERATION_ALLOW_PRIVATE_URLS` is therefore an explicit, default-off opt-in rather than a
loosening. Two properties matter and both are asserted: unset must behave exactly as before, and
a value that is not a deliberate yes must not count as one.
"""

from __future__ import annotations

import pytest

from panel_core.api.panels import _validate_panel_url


PRIVATE = (
    "https://10.0.0.5/panel",
    "https://192.168.2.88:8443/panel",
    "https://172.16.4.1/panel",
    "http://127.0.0.1:5000/panel",
    "https://node.internal/panel",
    "https://node.local/panel",
    "https://localhost/panel",
    "https://node/panel",
)

PUBLIC = (
    "https://node1.example.com/panel",
    "https://8.8.8.8/panel",
)

NOT_A_YES = ("", "0", "false", "no", "off", "maybe", "  ")


@pytest.fixture(autouse=True)
def _default_closed(monkeypatch):
    monkeypatch.delenv("FEDERATION_ALLOW_PRIVATE_URLS", raising=False)


@pytest.mark.parametrize("url", PRIVATE)
def test_a_private_address_is_refused_by_default(url):
    with pytest.raises(ValueError) as caught:
        _validate_panel_url(url)
    assert "FEDERATION_ALLOW_PRIVATE_URLS" in str(caught.value), (
        "the refusal does not say how to allow it deliberately, so an operator with a legitimate "
        "private topology has nothing to go on and reaches for the database instead."
    )


@pytest.mark.parametrize("url", PRIVATE)
def test_the_flag_opens_it(url, monkeypatch):
    monkeypatch.setenv("FEDERATION_ALLOW_PRIVATE_URLS", "true")
    assert _validate_panel_url(url) == url


@pytest.mark.parametrize("value", NOT_A_YES)
def test_only_a_deliberate_yes_counts(value, monkeypatch):
    """A variable left in `.env` as an empty placeholder must not read as consent."""

    monkeypatch.setenv("FEDERATION_ALLOW_PRIVATE_URLS", value)
    with pytest.raises(ValueError):
        _validate_panel_url("https://10.0.0.5/panel")


@pytest.mark.parametrize("url", PUBLIC)
def test_a_public_address_is_accepted_either_way(url, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])
    assert _validate_panel_url(url) == url
    monkeypatch.setenv("FEDERATION_ALLOW_PRIVATE_URLS", "true")
    assert _validate_panel_url(url) == url


@pytest.mark.parametrize("url", ("ftp://node1.example.com", "node1.example.com", "https://"))
def test_the_flag_does_not_excuse_a_malformed_url(url, monkeypatch):
    """The opt-in is about *where* the panel is, not about accepting anything at all."""

    monkeypatch.setenv("FEDERATION_ALLOW_PRIVATE_URLS", "true")
    with pytest.raises(ValueError):
        _validate_panel_url(url)


def test_a_domain_resolving_to_private_is_refused(monkeypatch):
    """The literal because a rebinder stops here is a name that *looks* public but resolves private."""

    monkeypatch.setattr("socket.getaddrinfo", lambda host, port: [(5, 1, 6, "", ("10.0.0.99", 0))])
    with pytest.raises(ValueError) as caught:
        _validate_panel_url("https://stolen.example/panel")
    assert "FEDERATION_ALLOW_PRIVATE_URLS" in str(caught.value)


def test_an_unresolvable_domain_is_not_refused_here(monkeypatch):
    """No resolution is not proof of malice: the handshake fails with its own clear error.

    Without this a transient DNS outage becomes a hard refusal at save time, and offline CI is
    broken by a check that was never meant to certify reachability.
    """

    import socket as _socket

    def _fail(host, port):
        raise _socket.gaierror("Name or service not known")

    monkeypatch.setattr("socket.getaddrinfo", _fail)
    assert _validate_panel_url("https://node1.example.com/panel") == "https://node1.example.com/panel"
