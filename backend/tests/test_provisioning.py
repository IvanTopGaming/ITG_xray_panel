from panel_core.services.provisioning import _generate_email, _generate_identity


def test_generate_identity_vless_returns_uuid_hex():

    identity = _generate_identity("vless")
    cleaned = identity.replace("-", "")
    assert len(cleaned) >= 32
    assert all(c in "0123456789abcdef" for c in cleaned)


def test_generate_identity_vmess_returns_uuid_hex():
    identity = _generate_identity("vmess")
    cleaned = identity.replace("-", "")
    assert len(cleaned) >= 32


def test_generate_identity_unknown_protocol_returns_token():

    identity = _generate_identity("trojan")
    assert len(identity) >= 16
    assert all(c.isalnum() or c in "-_" for c in identity)


def test_generate_identity_two_calls_return_different_values():
    a = _generate_identity("vless")
    b = _generate_identity("vless")
    assert a != b


def test_generate_email_includes_telegram_id_and_inbound():

    email = _generate_email(telegram_id=12345, inbound_tag="DE-vless")
    assert "12345" in email
    assert "DE-vless" in email
    assert email.startswith("tg")


import json as _json
import time as _time
import uuid as _uuid
from unittest.mock import patch

import pytest

from panel_core.models import Client, Inbound, Tariff, TariffItem
from panel_core.services.provisioning import apply_tariff_for_user, provision_single_item


@pytest.fixture
def basic_setup(app, db):

    inbound_de = Inbound(tag="DE-vless", protocol="vless", port=10001, stream_settings="{}")
    inbound_msk = Inbound(tag="MSK-vless", protocol="vless", port=10002, stream_settings="{}")
    db.session.add_all([inbound_de, inbound_msk])
    db.session.flush()

    tariff = Tariff(name="Standard", price_rub=150, period_days=30)
    db.session.add(tariff)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="DE-vless", traffic_gb=0, sort_order=0))
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="MSK-vless", traffic_gb=70, sort_order=1))
    db.session.commit()
    return tariff


def _make_client(db, *, telegram_id, inbound_tag, expiry_ms, limit_bytes, up=0, down=0):
    c = Client(
        id=str(_uuid.uuid4()),
        email=f"existing_{inbound_tag}",
        inbound_tag=inbound_tag,
        telegram_id=telegram_id,
        limit_bytes=limit_bytes,
        expiry_time=expiry_ms,
        up=up,
        down=down,
        enable=True,
    )
    db.session.add(c)
    db.session.commit()
    return c


def test_apply_extends_existing_client(app, db, basic_setup):
    tariff = basic_setup
    now_ms = int(_time.time() * 1000)
    pre_expiry = now_ms + 5 * 86400_000
    client = _make_client(
        db,
        telegram_id=42,
        inbound_tag="DE-vless",
        expiry_ms=pre_expiry,
        limit_bytes=10_000_000_000,
        up=2_000_000,
        down=3_000_000,
    )
    _make_client(
        db,
        telegram_id=42,
        inbound_tag="MSK-vless",
        expiry_ms=pre_expiry,
        limit_bytes=70 * 1024**3,
        up=1_000_000,
        down=1_500_000,
    )

    with patch("panel_core.services.provisioning._sync_after_provision"):
        result = apply_tariff_for_user(42, tariff, source="trial", operation_id="test-op")

    db.session.refresh(client)

    target = pre_expiry + 30 * 86400_000
    assert client.expiry_time == target
    assert client.up == 0
    assert client.down == 0
    assert client.limit_bytes == 0
    assert client.enable is True
    assert client.tariff_id == tariff.id

    assert "clients" in result
    assert result["expires_at_ms"] == target


def test_apply_extends_uses_now_when_expiry_already_past(app, db, basic_setup):
    tariff = basic_setup
    now_ms = int(_time.time() * 1000)
    past_expiry = now_ms - 86400_000
    _make_client(
        db,
        telegram_id=42,
        inbound_tag="DE-vless",
        expiry_ms=past_expiry,
        limit_bytes=0,
    )
    _make_client(
        db,
        telegram_id=42,
        inbound_tag="MSK-vless",
        expiry_ms=past_expiry,
        limit_bytes=0,
    )

    with patch("panel_core.services.provisioning._sync_after_provision"):
        result = apply_tariff_for_user(42, tariff, source="auto_renew", operation_id="test-op")

    expected = now_ms + 30 * 86400_000
    assert abs(result["expires_at_ms"] - expected) < 2_000


def test_apply_expiry_is_wall_clock_offset_from_purchase_time(app, db, basic_setup):

    one_day_tariff = Tariff(name="One day", price_rub=50, period_days=1)
    db.session.add(one_day_tariff)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=one_day_tariff.id, inbound_tag="DE-vless", traffic_gb=0, sort_order=0))
    db.session.commit()

    fixed_now_ms = int(_time.time() * 1000)
    with (
        patch("panel_core.services.provisioning.time.time", return_value=fixed_now_ms / 1000),
        patch("panel_core.services.provisioning._sync_after_provision"),
    ):
        result = apply_tariff_for_user(7777, one_day_tariff, source="trial", operation_id="test-op")

    assert result["expires_at_ms"] == fixed_now_ms + 86400_000


def test_apply_msk_item_sets_70gb_limit(app, db, basic_setup):

    tariff = basic_setup
    now_ms = int(_time.time() * 1000)
    msk = _make_client(
        db,
        telegram_id=42,
        inbound_tag="MSK-vless",
        expiry_ms=now_ms,
        limit_bytes=999,
    )
    _make_client(
        db,
        telegram_id=42,
        inbound_tag="DE-vless",
        expiry_ms=now_ms,
        limit_bytes=999,
    )

    with patch("panel_core.services.provisioning._sync_after_provision"):
        apply_tariff_for_user(42, tariff, source="trial", operation_id="test-op")

    db.session.refresh(msk)
    assert msk.limit_bytes == 70 * 1024**3


def test_apply_creates_missing_client(app, db, basic_setup):

    tariff = basic_setup

    with patch("panel_core.services.provisioning._sync_after_provision"):
        result = apply_tariff_for_user(99, tariff, source="trial", operation_id="test-op")

    clients = Client.query.filter_by(telegram_id=99).all()
    assert len(clients) == 2
    by_inbound = {c.inbound_tag: c for c in clients}
    assert "DE-vless" in by_inbound
    assert "MSK-vless" in by_inbound
    assert by_inbound["DE-vless"].limit_bytes == 0
    assert by_inbound["MSK-vless"].limit_bytes == 70 * 1024**3
    assert by_inbound["DE-vless"].tariff_id == tariff.id
    assert "-" in by_inbound["DE-vless"].id
    assert len(result["clients"]) == 2


def test_apply_creates_only_missing_when_partial_overlap(app, db, basic_setup):

    tariff = basic_setup
    now_ms = int(_time.time() * 1000)
    de_existing = _make_client(
        db,
        telegram_id=42,
        inbound_tag="DE-vless",
        expiry_ms=now_ms,
        limit_bytes=0,
    )

    with patch("panel_core.services.provisioning._sync_after_provision"):
        apply_tariff_for_user(42, tariff, source="trial", operation_id="test-op")

    clients = Client.query.filter_by(telegram_id=42).all()
    assert len(clients) == 2
    by_inbound = {c.inbound_tag: c for c in clients}

    assert by_inbound["DE-vless"].id == de_existing.id

    assert by_inbound["MSK-vless"].id != de_existing.id
    assert by_inbound["MSK-vless"].limit_bytes == 70 * 1024**3


def test_apply_handles_email_collision(app, db, basic_setup):

    tariff = basic_setup

    _make_client(
        db,
        telegram_id=999,
        inbound_tag="DE-vless",
        expiry_ms=0,
        limit_bytes=0,
    )
    db.session.query(Client).filter_by(telegram_id=999).update({"email": "tg99_DE-vless"})
    db.session.commit()

    with patch("panel_core.services.provisioning._sync_after_provision"):
        apply_tariff_for_user(99, tariff, source="trial", operation_id="test-op")

    new_clients = Client.query.filter_by(telegram_id=99).all()
    assert len(new_clients) == 2
    de_client = next(c for c in new_clients if c.inbound_tag == "DE-vless")
    assert de_client.email != "tg99_DE-vless"
    assert de_client.email.startswith("tg99_DE-vless_")


def test_provision_calls_xray_regen_once(app, db, basic_setup):

    tariff = basic_setup
    with (
        patch("panel_core.services.provisioning.generate_config_file") as mock_gen,
        patch("panel_core.services.provisioning.restart_xray_container") as mock_restart,
        patch("panel_core.services.provisioning._api_add_user_grpc", return_value=True),
        patch("panel_core.services.provisioning.sub_cache"),
    ):
        apply_tariff_for_user(99, tariff, source="trial", operation_id="test-op")

    assert mock_gen.call_count == 1
    assert mock_restart.call_count == 0


def test_provision_new_vless_uses_grpc_no_restart(app, db, basic_setup):

    tariff = basic_setup
    with (
        patch("panel_core.services.provisioning.generate_config_file") as mock_gen,
        patch("panel_core.services.provisioning.restart_xray_container") as mock_restart,
        patch("panel_core.services.provisioning._api_add_user_grpc", return_value=True) as mock_add,
        patch("panel_core.services.provisioning.sub_cache"),
    ):
        apply_tariff_for_user(99, tariff, source="trial", operation_id="test-op")

    assert mock_gen.call_count == 1
    assert mock_restart.call_count == 0
    assert mock_add.call_count == 2


def test_provision_extending_enabled_vless_skips_runtime(app, db, basic_setup):

    tariff = basic_setup
    now_ms = int(_time.time() * 1000)
    _make_client(db, telegram_id=42, inbound_tag="DE-vless", expiry_ms=now_ms, limit_bytes=0)
    _make_client(db, telegram_id=42, inbound_tag="MSK-vless", expiry_ms=now_ms, limit_bytes=0)

    with (
        patch("panel_core.services.provisioning.generate_config_file") as mock_gen,
        patch("panel_core.services.provisioning.restart_xray_container") as mock_restart,
        patch("panel_core.services.provisioning._api_add_user_grpc", return_value=True) as mock_add,
        patch("panel_core.services.provisioning.sub_cache"),
    ):
        apply_tariff_for_user(42, tariff, source="auto_renew", operation_id="test-op")

    assert mock_gen.call_count == 1
    assert mock_restart.call_count == 0
    assert mock_add.call_count == 0


def test_provision_extending_disabled_vless_re_adds_via_grpc(app, db, basic_setup):

    tariff = basic_setup
    now_ms = int(_time.time() * 1000)
    de = _make_client(db, telegram_id=42, inbound_tag="DE-vless", expiry_ms=now_ms, limit_bytes=0)
    _make_client(db, telegram_id=42, inbound_tag="MSK-vless", expiry_ms=now_ms, limit_bytes=0)
    de.enable = False
    db.session.commit()

    with (
        patch("panel_core.services.provisioning.generate_config_file"),
        patch("panel_core.services.provisioning.restart_xray_container") as mock_restart,
        patch("panel_core.services.provisioning._api_add_user_grpc", return_value=True) as mock_add,
        patch("panel_core.services.provisioning.sub_cache"),
    ):
        apply_tariff_for_user(42, tariff, source="auto_renew", operation_id="test-op")

    assert mock_restart.call_count == 0
    assert mock_add.call_count == 1


def test_provision_non_vless_inbound_requires_restart(app, db):

    inbound_ss = Inbound(tag="SS-1", protocol="shadowsocks", port=10003, stream_settings="{}")
    db.session.add(inbound_ss)
    db.session.flush()
    tariff = Tariff(name="SS", price_rub=100, period_days=30)
    db.session.add(tariff)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="SS-1", traffic_gb=0, sort_order=0))
    db.session.commit()

    with (
        patch("panel_core.services.provisioning.generate_config_file"),
        patch("panel_core.services.provisioning.restart_xray_container") as mock_restart,
        patch("panel_core.services.provisioning._api_add_user_grpc") as mock_add,
        patch("panel_core.services.provisioning.sub_cache"),
    ):
        apply_tariff_for_user(99, tariff, source="trial", operation_id="test-op")

    assert mock_restart.call_count == 1
    assert mock_add.call_count == 0


def test_provision_grpc_failure_falls_back_to_restart(app, db, basic_setup):

    tariff = basic_setup

    with (
        patch("panel_core.services.provisioning.generate_config_file"),
        patch("panel_core.services.provisioning.restart_xray_container") as mock_restart,
        patch("panel_core.services.provisioning._api_add_user_grpc", return_value=False),
        patch("panel_core.services.provisioning.sub_cache"),
    ):
        apply_tariff_for_user(99, tariff, source="trial", operation_id="test-op")

    assert mock_restart.call_count == 1


def _flow_tariff(db):
    inbound_xh = Inbound(
        tag="XH-vless",
        protocol="vless",
        port=10010,
        stream_settings=_json.dumps({"network": "xhttp", "security": "tls"}),
    )
    inbound_tcp = Inbound(
        tag="TCPR-vless",
        protocol="vless",
        port=10011,
        stream_settings=_json.dumps({"network": "tcp", "security": "reality"}),
    )
    db.session.add_all([inbound_xh, inbound_tcp])
    db.session.flush()
    tariff = Tariff(name="Flow", price_rub=100, period_days=30)
    db.session.add(tariff)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="XH-vless", traffic_gb=0, sort_order=0))
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="TCPR-vless", traffic_gb=0, sort_order=1))
    db.session.commit()
    return tariff


def test_apply_sets_flow_only_on_flow_compatible_inbounds(app, db):

    tariff = _flow_tariff(db)

    with patch("panel_core.services.provisioning._sync_after_provision"):
        apply_tariff_for_user(501, tariff, source="trial", operation_id="test-op")

    by_inbound = {c.inbound_tag: c for c in Client.query.filter_by(telegram_id=501).all()}
    assert by_inbound["XH-vless"].flow == ""
    assert by_inbound["TCPR-vless"].flow == "xtls-rprx-vision"


def test_provision_single_item_no_flow_on_xhttp_inbound(app, db):

    _flow_tariff(db)

    with patch("panel_core.services.provisioning._sync_after_provision"):
        provision_single_item(
            telegram_id=502,
            inbound_tag="XH-vless",
            expiry_ms=int(_time.time() * 1000) + 86_400_000,
            limit_bytes=0,
        )

    c = Client.query.filter_by(telegram_id=502, inbound_tag="XH-vless").first()
    assert c is not None
    assert c.flow == ""


def test_provision_single_item_vision_on_tcp_reality_inbound(app, db):

    _flow_tariff(db)

    with patch("panel_core.services.provisioning._sync_after_provision"):
        provision_single_item(
            telegram_id=503,
            inbound_tag="TCPR-vless",
            expiry_ms=int(_time.time() * 1000) + 86_400_000,
            limit_bytes=0,
        )

    c = Client.query.filter_by(telegram_id=503, inbound_tag="TCPR-vless").first()
    assert c is not None
    assert c.flow == "xtls-rprx-vision"


def _federated_tariff(db):
    inbound_local = Inbound(tag="LOC-vless", protocol="vless", port=10020, stream_settings="{}")
    db.session.add(inbound_local)
    db.session.flush()
    tariff = Tariff(name="Fed", price_rub=200, period_days=30)
    db.session.add(tariff)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="LOC-vless", traffic_gb=0, sort_order=0))
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="remote-vless", traffic_gb=0, sort_order=1, panel_id=7))
    db.session.commit()
    return tariff


def test_apply_remote_provision_happens_before_local_writes(app, db):

    tariff = _federated_tariff(db)
    pending_at_call = []

    def _spy(panel_id, telegram_id, inbound_tag, params):
        pending_at_call.append((len(db.session.new), len(db.session.dirty)))
        return {"status": "ok", "expires_at_ms": 1}

    with (
        patch("panel_core.services.panel_proxy.proxy_provision", side_effect=_spy),
        patch("panel_core.services.provisioning._sync_after_provision"),
    ):
        apply_tariff_for_user(601, tariff, source="trial", operation_id="test-op")

    assert pending_at_call == [(0, 0)]
    assert Client.query.filter_by(telegram_id=601, inbound_tag="LOC-vless").count() == 1


def test_apply_remote_failure_leaves_local_state_untouched(app, db):

    tariff = _federated_tariff(db)

    with (
        patch("panel_core.services.panel_proxy.proxy_provision", side_effect=RuntimeError("panel down")),
        patch("panel_core.services.provisioning._sync_after_provision"),
        pytest.raises(RuntimeError),
    ):
        apply_tariff_for_user(602, tariff, source="admin_grant", operation_id="test-op")

    assert len(db.session.new) == 0
    assert len(db.session.dirty) == 0
    assert Client.query.filter_by(telegram_id=602).count() == 0


def test_apply_clears_traffic_notifications_on_renewal(app, db, basic_setup):

    from panel_core.models import NotificationLog

    tariff = basic_setup

    existing = Client(
        id=str(_uuid.uuid4()),
        email="tg77_DE-vless",
        inbound_tag="DE-vless",
        telegram_id=77,
        tariff_id=tariff.id,
        up=900_000_000,
        down=200_000_000,
        limit_bytes=1_000_000_000,
        expiry_time=int(_time.time() * 1000) + 86_400_000,
        enable=True,
    )
    db.session.add(existing)
    db.session.flush()
    db.session.add_all(
        [
            NotificationLog(telegram_id=77, client_id=existing.id, kind="traffic_80"),
            NotificationLog(telegram_id=77, client_id=existing.id, kind="traffic_95"),
            NotificationLog(telegram_id=77, client_id=existing.id, kind="expiry_1d"),
        ]
    )
    db.session.commit()

    with patch("panel_core.services.provisioning._sync_after_provision"):
        apply_tariff_for_user(77, tariff, source="auto_renew", operation_id="test-op")

    remaining = NotificationLog.query.filter_by(client_id=existing.id).all()
    kinds = {n.kind for n in remaining}
    assert "traffic_80" not in kinds
    assert "traffic_95" not in kinds
    assert "traffic_exhausted" not in kinds
    assert "expiry_1d" not in kinds


def test_apply_extend_clears_expiry_notification_log(app, db, basic_setup):
    from panel_core.models import NotificationLog

    tariff = basic_setup
    now_ms = int(_time.time() * 1000)
    client = _make_client(
        db,
        telegram_id=42,
        inbound_tag="DE-vless",
        expiry_ms=now_ms + 5 * 86400_000,
        limit_bytes=10_000_000_000,
    )
    db.session.add(NotificationLog(telegram_id=42, client_id=client.id, kind="expiry_3d"))
    db.session.add(NotificationLog(telegram_id=42, client_id=client.id, kind="expired"))
    db.session.commit()

    with patch("panel_core.services.provisioning._sync_after_provision"):
        apply_tariff_for_user(42, tariff, source="trial", operation_id="test-op")

    assert NotificationLog.query.filter_by(client_id=client.id, kind="expiry_3d").count() == 0
    assert NotificationLog.query.filter_by(client_id=client.id, kind="expired").count() == 0
