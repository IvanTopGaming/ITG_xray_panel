"""Unit tests for app.services.device_tracking."""

import time
import uuid


from app.models import Client, ClientDevice, Inbound
from app.services.device_tracking import device_gate, list_devices, revoke_device


def _make_inbound(db, *, tag="DE-vless", device_limit=0, port=10001):
    inbound = Inbound(tag=tag, protocol="vless", port=port, stream_settings="{}", device_limit=device_limit)
    db.session.add(inbound)
    db.session.flush()
    return inbound


def _make_client(db, *, inbound_tag="DE-vless", device_limit=None, telegram_id=42):
    client = Client(
        id=str(uuid.uuid4()),
        email=f"tg{telegram_id}_{inbound_tag}",
        inbound_tag=inbound_tag,
        telegram_id=telegram_id,
        limit_bytes=0,
        expiry_time=0,
        enable=True,
        device_limit=device_limit,
    )
    db.session.add(client)
    db.session.flush()
    return client


def _make_device(db, *, client_id, hwid, last_seen=None):
    now_ms = last_seen or int(time.time() * 1000)
    device = ClientDevice(
        client_id=client_id,
        hwid=hwid,
        first_seen=now_ms,
        last_seen=now_ms,
        hits=1,
    )
    db.session.add(device)
    db.session.flush()
    return device


# ---------- list_devices ----------


def test_list_devices_returns_devices_ordered_by_last_seen_desc(app, db):
    _make_inbound(db)
    client = _make_client(db)
    _make_device(db, client_id=client.id, hwid="aaa", last_seen=1000)
    _make_device(db, client_id=client.id, hwid="bbb", last_seen=3000)
    _make_device(db, client_id=client.id, hwid="ccc", last_seen=2000)
    db.session.commit()

    devices = list_devices(client.id)
    assert [d.hwid for d in devices] == ["bbb", "ccc", "aaa"]


def test_list_devices_returns_empty_list_for_no_devices(app, db):
    _make_inbound(db)
    client = _make_client(db)
    db.session.commit()

    devices = list_devices(client.id)
    assert devices == []


# ---------- revoke_device ----------


def test_revoke_device_deletes_and_returns_true(app, db):
    _make_inbound(db)
    client = _make_client(db)
    device = _make_device(db, client_id=client.id, hwid="hw1")
    db.session.commit()

    assert revoke_device(client.id, device.id) is True
    assert ClientDevice.query.filter_by(id=device.id).first() is None


def test_revoke_device_returns_false_for_nonexistent(app, db):
    _make_inbound(db)
    client = _make_client(db)
    db.session.commit()

    assert revoke_device(client.id, 9999) is False


def test_revoke_device_returns_false_for_wrong_client(app, db):
    _make_inbound(db)
    client_a = _make_client(db, telegram_id=1)
    client_b = _make_client(db, telegram_id=2)
    device = _make_device(db, client_id=client_a.id, hwid="hw1")
    db.session.commit()

    assert revoke_device(client_b.id, device.id) is False
    # Device still exists on the original client.
    assert ClientDevice.query.filter_by(id=device.id).first() is not None


# ---------- device_gate ----------


def test_device_gate_first_access_creates_device_and_returns_ok(app, db):
    inbound = _make_inbound(db, device_limit=3)
    client = _make_client(db)
    db.session.commit()

    state, headers = device_gate(client, inbound, {"x-hwid": "new-hwid-1"})

    assert state == "ok"
    assert headers.get("x-hwid-active") == "true"
    device = ClientDevice.query.filter_by(client_id=client.id, hwid="new-hwid-1").first()
    assert device is not None
    assert device.hits == 1


def test_device_gate_same_hwid_returns_ok_and_updates_last_seen(app, db):
    inbound = _make_inbound(db, device_limit=3)
    client = _make_client(db)
    device = _make_device(db, client_id=client.id, hwid="existing-hwid", last_seen=1000)
    db.session.commit()

    state, headers = device_gate(client, inbound, {"x-hwid": "existing-hwid"})

    assert state == "ok"
    db.session.refresh(device)
    assert device.last_seen > 1000
    assert device.hits == 2


def test_device_gate_new_hwid_at_limit_returns_blocked(app, db):
    inbound = _make_inbound(db, device_limit=2)
    client = _make_client(db)
    _make_device(db, client_id=client.id, hwid="d1")
    _make_device(db, client_id=client.id, hwid="d2")
    db.session.commit()

    state, headers = device_gate(client, inbound, {"x-hwid": "d3"})

    assert state == "limit"
    assert headers.get("x-hwid-max-devices-reached") == "true"
    # No new device row was created.
    assert ClientDevice.query.filter_by(client_id=client.id, hwid="d3").first() is None


def test_device_gate_unlimited_allows_any_number_of_devices(app, db):
    inbound = _make_inbound(db, device_limit=0)
    client = _make_client(db)
    for i in range(10):
        _make_device(db, client_id=client.id, hwid=f"dev{i}")
    db.session.commit()

    state, headers = device_gate(client, inbound, {"x-hwid": "dev-new"})

    assert state == "ok"
    assert ClientDevice.query.filter_by(client_id=client.id, hwid="dev-new").first() is not None


def test_device_gate_inherits_inbound_device_limit(app, db):
    """When client.device_limit is None, the inbound's limit applies."""
    inbound = _make_inbound(db, device_limit=1)
    client = _make_client(db, device_limit=None)
    _make_device(db, client_id=client.id, hwid="sole-device")
    db.session.commit()

    state, _ = device_gate(client, inbound, {"x-hwid": "another-device"})

    assert state == "limit"


def test_device_gate_client_limit_overrides_inbound_limit(app, db):
    """client.device_limit takes precedence over inbound.device_limit."""
    inbound = _make_inbound(db, device_limit=1)
    client = _make_client(db, device_limit=3)
    _make_device(db, client_id=client.id, hwid="d1")
    db.session.commit()

    # Inbound says 1, but client says 3 — second device should be allowed.
    state, _ = device_gate(client, inbound, {"x-hwid": "d2"})
    assert state == "ok"
    assert ClientDevice.query.filter_by(client_id=client.id, hwid="d2").first() is not None


def test_device_gate_client_limit_zero_overrides_inbound_limit(app, db):
    """client.device_limit=0 means unlimited, even if inbound has a limit."""
    inbound = _make_inbound(db, device_limit=1)
    client = _make_client(db, device_limit=0)
    _make_device(db, client_id=client.id, hwid="d1")
    db.session.commit()

    state, _ = device_gate(client, inbound, {"x-hwid": "d2"})
    assert state == "ok"


def test_device_gate_no_hwid_with_limit_returns_unsupported(app, db):
    """Missing HWID + nonzero limit should return 'unsupported'."""
    inbound = _make_inbound(db, device_limit=2)
    client = _make_client(db)
    db.session.commit()

    state, headers = device_gate(client, inbound, {})

    assert state == "unsupported"
    assert headers.get("x-hwid-not-supported") == "true"


def test_device_gate_no_hwid_unlimited_returns_ok(app, db):
    """Missing HWID + no limit (0) should pass through silently."""
    inbound = _make_inbound(db, device_limit=0)
    client = _make_client(db)
    db.session.commit()

    state, headers = device_gate(client, inbound, {})

    assert state == "ok"
    assert headers == {}


# ---------- user_device_gate ----------


def _enable_user_device_limit(db, limit):
    from app.models import SystemSetting

    db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
    db.session.add(SystemSetting(key="device_limit_per_user", value=str(limit)))
    db.session.commit()


def test_user_gate_off_passes(app, db):
    from app.services.device_tracking import user_device_gate

    _make_inbound(db)
    _make_client(db, telegram_id=70)
    db.session.commit()
    state, _ = user_device_gate(70, {"x-hwid": "dev1"})
    assert state == "ok"


def test_user_gate_no_hwid_with_limit_unsupported(app, db):
    from app.services.device_tracking import user_device_gate

    _make_inbound(db)
    _make_client(db, telegram_id=71)
    _enable_user_device_limit(db, 2)
    state, hdrs = user_device_gate(71, {"x-hwid": ""})
    assert state == "unsupported"
    assert hdrs.get("x-hwid-not-supported") == "true"


def test_user_gate_registers_then_touches(app, db):
    from app.models import ClientDevice
    from app.services.device_tracking import user_device_gate

    _make_inbound(db)
    c = _make_client(db, telegram_id=72)
    _enable_user_device_limit(db, 2)
    s1, _ = user_device_gate(72, {"x-hwid": "devA"})
    assert s1 == "ok"
    assert ClientDevice.query.filter_by(client_id=c.id, hwid="devA").count() == 1
    s2, _ = user_device_gate(72, {"x-hwid": "devA"})
    assert s2 == "ok"
    assert ClientDevice.query.filter_by(hwid="devA").count() == 1


def test_user_gate_blocks_over_limit(app, db):
    from app.services.device_tracking import user_device_gate

    _make_inbound(db)
    _make_client(db, telegram_id=73)
    _enable_user_device_limit(db, 1)
    assert user_device_gate(73, {"x-hwid": "devA"})[0] == "ok"
    state, hdrs = user_device_gate(73, {"x-hwid": "devB"})
    assert state == "limit"
    assert hdrs.get("x-hwid-max-devices-reached") == "true"


def test_user_gate_counts_across_multiple_keys(app, db):
    from app.services.device_tracking import user_device_gate

    _make_inbound(db, tag="DE-vless")
    _make_inbound(db, tag="NL-vless", port=10002)
    _make_client(db, inbound_tag="DE-vless", telegram_id=74)
    _make_client(db, inbound_tag="NL-vless", telegram_id=74)
    _enable_user_device_limit(db, 1)
    assert user_device_gate(74, {"x-hwid": "devA"})[0] == "ok"
    assert user_device_gate(74, {"x-hwid": "devB"})[0] == "limit"


# ---------- subscription_device_settings ----------


def test_subscription_device_settings_defaults(app, db):
    from app.services.device_tracking import subscription_device_settings

    enabled, limit = subscription_device_settings()
    assert enabled is False and limit == 0


def test_subscription_device_settings_reads_values(app, db):
    from app.models import SystemSetting
    from app.services.device_tracking import subscription_device_settings

    db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
    db.session.add(SystemSetting(key="device_limit_per_user", value="4"))
    db.session.commit()
    enabled, limit = subscription_device_settings()
    assert enabled is True and limit == 4
