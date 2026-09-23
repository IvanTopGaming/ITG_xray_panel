from urllib.parse import unquote, urlsplit

import pytest

from panel_core.services.sub_links import build_client_sub_url


@pytest.mark.parametrize("client_id", ["password#tail", "password?query", "a/b+c=", "name%2Fvalue"])
def test_subscription_link_preserves_entire_client_id(monkeypatch, client_id):
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.com")
    parsed = urlsplit(build_client_sub_url(client_id))
    assert not parsed.query
    assert not parsed.fragment
    assert unquote(parsed.path.removeprefix("/api/sub/")) == client_id
