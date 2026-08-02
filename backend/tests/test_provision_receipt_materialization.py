import json as _json
import time as _time
from unittest.mock import patch

import pytest

from panel_core.models import Client, Inbound, ProvisionReceipt
from panel_core.services.provisioning import provision_single_item


def _inbound(db):
    inbound = Inbound(
        tag="DE-vless",
        protocol="vless",
        port=10020,
        stream_settings=_json.dumps({"network": "tcp", "security": "reality"}),
    )
    db.session.add(inbound)
    db.session.commit()
    return inbound


def test_receipt_is_not_materialised_when_the_sync_fails(app, db):
    _inbound(db)

    with patch("panel_core.services.provisioning._sync_after_provision", side_effect=RuntimeError("xray down")):
        with pytest.raises(RuntimeError):
            provision_single_item(
                telegram_id=900,
                inbound_tag="DE-vless",
                period_ms=86_400_000,
                limit_bytes=0,
                idempotency_key="pay:900",
            )

    receipt = ProvisionReceipt.query.filter_by(idempotency_key="pay:900", inbound_tag="DE-vless").first()
    assert receipt is not None
    assert receipt.materialized is False
    assert Client.query.filter_by(telegram_id=900, inbound_tag="DE-vless").first() is not None


def test_replay_after_a_failed_sync_syncs_and_marks_the_receipt(app, db):
    _inbound(db)

    with patch("panel_core.services.provisioning._sync_after_provision", side_effect=RuntimeError("xray down")):
        with pytest.raises(RuntimeError):
            provision_single_item(
                telegram_id=901,
                inbound_tag="DE-vless",
                period_ms=86_400_000,
                limit_bytes=0,
                idempotency_key="pay:901",
            )

    with patch("panel_core.services.provisioning._sync_after_provision") as sync:
        result = provision_single_item(
            telegram_id=901,
            inbound_tag="DE-vless",
            period_ms=86_400_000,
            limit_bytes=0,
            idempotency_key="pay:901",
        )
        assert sync.call_count == 1

    receipt = ProvisionReceipt.query.filter_by(idempotency_key="pay:901", inbound_tag="DE-vless").first()
    assert receipt.materialized is True
    assert result["expires_at_ms"] > int(_time.time() * 1000)


def test_replay_of_a_materialised_receipt_leaves_xray_alone(app, db):
    _inbound(db)

    with patch("panel_core.services.provisioning._sync_after_provision"):
        first = provision_single_item(
            telegram_id=902,
            inbound_tag="DE-vless",
            period_ms=86_400_000,
            limit_bytes=0,
            idempotency_key="pay:902",
        )

    with patch("panel_core.services.provisioning._sync_after_provision") as sync:
        second = provision_single_item(
            telegram_id=902,
            inbound_tag="DE-vless",
            period_ms=86_400_000,
            limit_bytes=0,
            idempotency_key="pay:902",
        )
        assert sync.call_count == 0

    assert second["expires_at_ms"] == first["expires_at_ms"]


def test_replay_adds_no_second_period(app, db):
    _inbound(db)

    with patch("panel_core.services.provisioning._sync_after_provision"):
        first = provision_single_item(
            telegram_id=903,
            inbound_tag="DE-vless",
            period_ms=86_400_000,
            limit_bytes=0,
            idempotency_key="pay:903",
        )
        second = provision_single_item(
            telegram_id=903,
            inbound_tag="DE-vless",
            period_ms=86_400_000,
            limit_bytes=0,
            idempotency_key="pay:903",
        )

    assert second["expires_at_ms"] == first["expires_at_ms"]
    client = Client.query.filter_by(telegram_id=903, inbound_tag="DE-vless").first()
    assert client.expiry_time == first["expires_at_ms"]
