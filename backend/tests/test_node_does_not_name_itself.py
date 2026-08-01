"""A node stopped putting its own name on the wire (wave 9, §97).

It used to read the `panel_name` setting twice — once for the handshake reply, once for every
snapshot — and the master discarded both: `_handshake` takes only `federation_token` out of the
reply, and a panel's card shows `LinkedPanel.name`, typed by the admin who added it. The setting
itself did not disappear; it became the master's, where it is the name a node shows on its own
System → Link card.
"""

import time

import pytest

from panel_core.models import FederationConfig, SystemSetting


@pytest.fixture
def app_with_federation(app):
    from panel_core.api import federation

    if not any(bp.name == "federation" for bp in app.blueprints.values()):
        app.register_blueprint(federation.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app_with_federation):
    return app_with_federation.test_client()


@pytest.fixture
def linked_config(db):
    cfg = db.session.get(FederationConfig, 1)
    cfg.federation_token = "test-federation-token-abc"
    cfg.master_url = "https://master.example.com"
    cfg.master_name = "Master"
    cfg.link_token = "used-token"
    cfg.link_token_used = True
    cfg.linked_at = int(time.time() * 1000)
    db.session.commit()
    return cfg


@pytest.fixture
def federation_headers(linked_config):
    return {"X-Federation-Token": linked_config.federation_token}


def test_snapshot_carries_no_panel_name(client, federation_headers):
    body = client.get("/api/federation/snapshot", headers=federation_headers).get_json()

    assert "panel_name" not in body
    assert "app_version" in body
    assert body["status"] == "ok"


def test_snapshot_carries_no_name_even_when_the_setting_exists(client, db, federation_headers):
    db.session.add(SystemSetting(key="panel_name", value="DE-1"))
    db.session.commit()

    body = client.get("/api/federation/snapshot", headers=federation_headers).get_json()

    assert "DE-1" not in str(body)


def test_handshake_reply_carries_no_name(client, db):
    cfg = db.session.get(FederationConfig, 1)
    cfg.link_token = "pending-token"
    cfg.link_token_used = False
    cfg.federation_token = None
    cfg.linked_at = None
    db.session.commit()

    body = client.post(
        "/api/federation/handshake",
        json={
            "link_token": "pending-token",
            "master_url": "https://master.example",
            "master_name": "Master",
        },
    ).get_json()

    assert "name" not in body
    assert body["federation_token"]
    assert "inbound_count" in body
