"""Tests for LinkedPanel and FederationConfig models (multi-panel federation)."""

import time

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import FederationConfig, LinkedPanel


# ─── LinkedPanel ──────────────────────────────────────────────────────────────


def test_linked_panel_creation(app, db):
    """A LinkedPanel row can be created and retrieved by id."""
    now = int(time.time())
    panel = LinkedPanel(
        name="eu-node",
        url="https://eu.example.com",
        federation_token="secret-token-abc",
        created_at=now,
    )
    db.session.add(panel)
    db.session.commit()

    fetched = LinkedPanel.query.filter_by(name="eu-node").first()
    assert fetched is not None
    assert fetched.url == "https://eu.example.com"
    assert fetched.federation_token == "secret-token-abc"
    assert fetched.status == "unknown"
    assert fetched.enable is True
    assert fetched.last_poll is None
    assert fetched.last_error is None
    assert fetched.created_at == now


def test_linked_panel_name_unique_constraint(app, db):
    """Two LinkedPanel rows with the same name must raise IntegrityError."""
    now = int(time.time())
    db.session.add(LinkedPanel(name="dup", url="https://a.com", federation_token="tok1", created_at=now))
    db.session.commit()

    db.session.add(LinkedPanel(name="dup", url="https://b.com", federation_token="tok2", created_at=now))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_linked_panel_to_dict_masks_token_by_default(app, db):
    """to_dict() must redact federation_token when mask_token=True (default)."""
    now = int(time.time())
    panel = LinkedPanel(
        name="masked-panel",
        url="https://secret.example.com",
        federation_token="super-secret-value",
        created_at=now,
    )
    db.session.add(panel)
    db.session.commit()

    d = panel.to_dict()
    assert d["federation_token"] == "••••••••"
    assert d["name"] == "masked-panel"
    assert d["url"] == "https://secret.example.com"
    assert d["enable"] is True
    assert d["status"] == "unknown"
    assert d["last_poll"] is None
    assert d["last_error"] is None
    assert d["created_at"] == now


def test_linked_panel_to_dict_unmask_token(app, db):
    """to_dict(mask_token=False) returns the real federation_token."""
    now = int(time.time())
    panel = LinkedPanel(
        name="unmasked-panel",
        url="https://open.example.com",
        federation_token="plain-text-token",
        created_at=now,
    )
    db.session.add(panel)
    db.session.commit()

    d = panel.to_dict(mask_token=False)
    assert d["federation_token"] == "plain-text-token"


def test_linked_panel_status_and_error_fields(app, db):
    """status, last_poll, and last_error can be updated and persist."""
    now = int(time.time())
    panel = LinkedPanel(
        name="live-panel",
        url="https://live.example.com",
        federation_token="tok",
        status="online",
        last_poll=now,
        last_error="",
        created_at=now,
    )
    db.session.add(panel)
    db.session.commit()

    fetched = LinkedPanel.query.filter_by(name="live-panel").first()
    assert fetched.status == "online"
    assert fetched.last_poll == now
    assert fetched.last_error == ""


def test_linked_panel_enable_defaults_true(app, db):
    """enable defaults to True for new LinkedPanel rows."""
    now = int(time.time())
    panel = LinkedPanel(name="default-enable", url="https://x.com", federation_token="t", created_at=now)
    db.session.add(panel)
    db.session.commit()
    db.session.refresh(panel)
    assert panel.enable is True


def test_linked_panel_can_be_disabled(app, db):
    """enable=False persists correctly."""
    now = int(time.time())
    panel = LinkedPanel(
        name="disabled-panel",
        url="https://off.example.com",
        federation_token="tok",
        enable=False,
        created_at=now,
    )
    db.session.add(panel)
    db.session.commit()
    db.session.refresh(panel)
    assert panel.enable is False
    assert panel.to_dict()["enable"] is False


# ─── FederationConfig ─────────────────────────────────────────────────────────


def test_federation_config_singleton_seeded_by_fixture(app, db):
    """conftest.py seeds id=1; querying it must return exactly one row."""
    rows = FederationConfig.query.all()
    assert len(rows) == 1
    assert rows[0].id == 1


def test_federation_config_defaults_are_null(app, db):
    """The freshly seeded singleton has all nullable fields as None/False."""
    cfg = db.session.get(FederationConfig, 1)
    assert cfg is not None
    assert cfg.master_url is None
    assert cfg.master_name is None
    assert cfg.federation_token is None
    assert cfg.link_token is None
    assert cfg.link_token_used is False
    assert cfg.linked_at is None


def test_federation_config_can_be_updated(app, db):
    """Fields on the singleton row can be written and read back."""
    now = int(time.time())
    cfg = db.session.get(FederationConfig, 1)
    cfg.master_url = "https://master.example.com"
    cfg.master_name = "Main Panel"
    cfg.federation_token = "fed-token-xyz"
    cfg.link_token = "link-abc"
    cfg.link_token_used = True
    cfg.linked_at = now
    db.session.commit()

    db.session.expire(cfg)
    refreshed = db.session.get(FederationConfig, 1)
    assert refreshed.master_url == "https://master.example.com"
    assert refreshed.master_name == "Main Panel"
    assert refreshed.federation_token == "fed-token-xyz"
    assert refreshed.link_token == "link-abc"
    assert refreshed.link_token_used is True
    assert refreshed.linked_at == now


def test_federation_config_singleton_constraint_rejects_second_row(app, db):
    """The CHECK constraint prevents inserting a second row (id != 1)."""
    db.session.add(FederationConfig(id=2))
    with pytest.raises(Exception):
        db.session.commit()
    db.session.rollback()
