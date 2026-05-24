"""Inbound.master_disabled behavior — added in schema v14.

A master_disabled inbound:
  - exists in master.db (so provisioning works)
  - is skipped by generate_config_file() → master Xray doesn't open the port
  - is included normally when pushed to a node via sync_inbound_to_node
  - is omitted from the master entry in subscription URLs (only node entries)
"""

from unittest.mock import patch

import pytest

from app.models import Client, Inbound


@pytest.fixture
def inbound_master_only(app, db):
    ib = Inbound(
        tag="okins-vless",
        port=443,
        protocol="vless",
        stream_settings='{"network":"tcp","security":"none"}',
        master_disabled=False,
    )
    db.session.add(ib)
    db.session.commit()
    return ib


@pytest.fixture
def inbound_nodes_only(app, db):
    ib = Inbound(
        tag="gw-vless",
        port=8443,
        protocol="vless",
        stream_settings='{"network":"tcp","security":"none"}',
        master_disabled=True,
    )
    db.session.add(ib)
    db.session.commit()
    return ib


def test_master_disabled_column_defaults_false(app, db):
    """Inserting an Inbound without master_disabled defaults to False."""
    ib = Inbound(tag="t", port=12345, protocol="vless", stream_settings="{}")
    db.session.add(ib)
    db.session.commit()
    db.session.refresh(ib)
    assert ib.master_disabled is False


def test_master_disabled_persists(app, db, inbound_nodes_only):
    db.session.refresh(inbound_nodes_only)
    assert inbound_nodes_only.master_disabled is True


def test_node_sync_payload_omits_master_disabled(app, db, inbound_nodes_only):
    """The flat payload pushed to a node must not carry master_disabled —
    nodes always run the inbound when they receive one."""
    from app.services.node_sync import _inbound_payload

    payload = _inbound_payload(inbound_nodes_only)
    assert "master_disabled" not in payload
    assert payload["tag"] == "gw-vless"
    assert payload["port"] == 8443


def test_subscription_master_visible_helper_respects_master_disabled(app, db, inbound_master_only, inbound_nodes_only):
    """_master_visible_to_client returns False whenever the inbound has
    master_disabled=True, irrespective of allowed_node_groups."""
    from app.api.subscription import _master_visible_to_client

    client = Client(
        id="11111111-1111-1111-1111-111111111111",
        email="user@x",
        inbound_tag="gw-vless",
        allowed_node_groups="",
    )
    db.session.add(client)
    db.session.commit()

    assert _master_visible_to_client(client, inbound_master_only) is True
    assert _master_visible_to_client(client, inbound_nodes_only) is False


def test_subscription_content_omits_master_link_for_master_disabled(app, db, inbound_nodes_only):
    """get_subscription_content returns only node-side links when the
    inbound is master_disabled (no vless:// for the master host)."""
    from app.api.subscription import get_subscription_content

    client = Client(
        id="22222222-2222-2222-2222-222222222222",
        email="user@y",
        inbound_tag="gw-vless",
        enable=True,
        flow="",
    )
    db.session.add(client)
    db.session.commit()

    with patch("app.api.subscription._get_remote_links", return_value=["vless://node-link"]):
        links = get_subscription_content(str(client.id))

    assert links == ["vless://node-link"]


def test_subscription_content_includes_master_when_enabled(app, db, inbound_master_only):
    from app.api.subscription import get_subscription_content

    client = Client(
        id="33333333-3333-3333-3333-333333333333",
        email="user@z",
        inbound_tag="okins-vless",
        enable=True,
        flow="",
    )
    db.session.add(client)
    db.session.commit()

    with patch("app.api.subscription._get_remote_links", return_value=[]):
        links = get_subscription_content(str(client.id))

    assert links is not None and len(links) == 1
    assert links[0].startswith("vless://")
