"""The device ledger is keyed by the Telegram account, not by the key.

Wave 3b moved subscriptions to the sub role alone, and that role holds no `Client` rows for
clients issued on nodes -- `Client` has no `panel_id` and the master mirrors none of them. A
budget counted through a join on `Client` therefore counts zero there, silently, which is why
`UserDevice.telegram_id` is the only thing the gate looks at now.
"""

import uuid

from panel_core.models import Client, Inbound, SystemSetting, UserDevice
from panel_core.services.device_tracking import (
    count_user_devices,
    device_counts_by_user,
    list_user_devices,
    revoke_user_device,
    subscription_device_settings,
    user_device_gate,
)


def _make_inbound(db, *, tag="DE-vless", device_limit=1, port=10001):
    inbound = Inbound(tag=tag, protocol="vless", port=port, stream_settings="{}", device_limit=device_limit)
    db.session.add(inbound)
    db.session.flush()
    return inbound


def _make_client(db, *, inbound_tag="DE-vless", telegram_id=42):
    client = Client(
        id=str(uuid.uuid4()),
        email=f"tg{telegram_id}_{inbound_tag}",
        inbound_tag=inbound_tag,
        telegram_id=telegram_id,
        limit_bytes=0,
        expiry_time=0,
        enable=True,
    )
    db.session.add(client)
    db.session.flush()
    return client


def _make_device(db, *, telegram_id, hwid, last_seen=1000):
    device = UserDevice(
        telegram_id=telegram_id,
        hwid=hwid,
        first_seen=last_seen,
        last_seen=last_seen,
        hits=1,
    )
    db.session.add(device)
    db.session.flush()
    return device


def _enable_user_device_limit(db, limit):
    db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
    db.session.add(SystemSetting(key="device_limit_per_user", value=str(limit)))
    db.session.commit()


def test_list_user_devices_orders_by_last_seen_desc(app, db):
    _make_device(db, telegram_id=1, hwid="older", last_seen=1000)
    _make_device(db, telegram_id=1, hwid="newer", last_seen=2000)
    db.session.commit()

    assert [d.hwid for d in list_user_devices(1)] == ["newer", "older"]


def test_list_user_devices_is_empty_without_devices(app, db):
    assert list_user_devices(1) == []


def test_list_user_devices_without_a_telegram_id_is_empty(app, db):
    _make_device(db, telegram_id=1, hwid="d1")
    db.session.commit()

    assert list_user_devices(None) == []


def test_revoke_user_device_deletes_and_returns_true(app, db):
    device = _make_device(db, telegram_id=1, hwid="d1")
    db.session.commit()

    assert revoke_user_device(1, device.id) is True
    assert UserDevice.query.filter_by(id=device.id).first() is None


def test_revoke_user_device_refuses_another_users_device(app, db):
    device = _make_device(db, telegram_id=1, hwid="d1")
    db.session.commit()

    assert revoke_user_device(2, device.id) is False
    assert UserDevice.query.filter_by(id=device.id).first() is not None


def test_revoke_user_device_returns_false_for_nonexistent(app, db):
    assert revoke_user_device(1, 9999) is False


def test_device_counts_by_user_groups_by_account(app, db):
    _make_device(db, telegram_id=1, hwid="a")
    _make_device(db, telegram_id=1, hwid="b")
    _make_device(db, telegram_id=2, hwid="c")
    db.session.commit()

    assert device_counts_by_user() == {1: 2, 2: 1}


def test_gate_passes_when_the_limit_is_off(app, db):
    state, headers = user_device_gate(70, {"x-hwid": "dev1"})

    assert state == "ok"
    assert headers == {}
    assert UserDevice.query.count() == 0


def test_gate_passes_without_a_telegram_id(app, db):
    _enable_user_device_limit(db, 1)

    state, headers = user_device_gate(None, {"x-hwid": "dev1"})

    assert state == "ok"
    assert UserDevice.query.count() == 0


def test_gate_reports_unsupported_when_the_client_sends_no_hwid(app, db):
    _enable_user_device_limit(db, 2)

    state, headers = user_device_gate(71, {"x-hwid": ""})

    assert state == "unsupported"
    assert headers.get("x-hwid-not-supported") == "true"


def test_gate_passes_without_hwid_when_the_limit_is_zero(app, db):
    db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
    db.session.add(SystemSetting(key="device_limit_per_user", value="0"))
    db.session.commit()

    state, headers = user_device_gate(71, {"x-hwid": ""})

    assert state == "ok"
    assert headers == {}


def test_gate_registers_then_touches_the_same_device(app, db):
    _enable_user_device_limit(db, 2)

    assert user_device_gate(72, {"x-hwid": "devA"})[0] == "ok"
    assert UserDevice.query.filter_by(telegram_id=72, hwid="devA").count() == 1

    assert user_device_gate(72, {"x-hwid": "devA"})[0] == "ok"
    rows = UserDevice.query.filter_by(telegram_id=72, hwid="devA").all()
    assert len(rows) == 1
    assert rows[0].hits == 2


def test_gate_blocks_a_new_device_over_the_limit(app, db):
    _enable_user_device_limit(db, 1)

    assert user_device_gate(73, {"x-hwid": "devA"})[0] == "ok"
    state, headers = user_device_gate(73, {"x-hwid": "devB"})

    assert state == "limit"
    assert headers.get("x-hwid-max-devices-reached") == "true"
    assert UserDevice.query.filter_by(telegram_id=73, hwid="devB").count() == 0


def test_the_budget_is_one_per_account_no_matter_how_many_keys(app, db):
    """This is the whole point of the ledger's grain.

    Before wave 3b the count went through a join on `Client`, so a user with keys on three
    nodes had three independent budgets -- each node counted only its own clients.
    """
    _make_inbound(db, tag="DE-vless")
    _make_inbound(db, tag="NL-vless", port=10002)
    _make_client(db, inbound_tag="DE-vless", telegram_id=74)
    _make_client(db, inbound_tag="NL-vless", telegram_id=74)
    _enable_user_device_limit(db, 1)

    assert user_device_gate(74, {"x-hwid": "devA"})[0] == "ok"
    assert user_device_gate(74, {"x-hwid": "devB"})[0] == "limit"


def test_the_gate_counts_devices_with_no_client_row_at_all(app, db):
    """The case the join could never serve: a user whose keys all live on nodes.

    On the sub role `Client.query` returns nothing for such a user, so the old join yielded
    an empty budget and the limit never fired.
    """
    _enable_user_device_limit(db, 2)

    assert Client.query.filter_by(telegram_id=75).count() == 0
    assert user_device_gate(75, {"x-hwid": "devA"})[0] == "ok"
    assert user_device_gate(75, {"x-hwid": "devB"})[0] == "ok"
    assert count_user_devices(75) == 2
    assert user_device_gate(75, {"x-hwid": "devC"})[0] == "limit"


def test_the_gate_records_what_the_client_reported(app, db):
    _enable_user_device_limit(db, 2)

    user_device_gate(
        76,
        {
            "x-hwid": "devA",
            "x-device-os": "android",
            "x-ver-os": "14",
            "x-device-model": "Pixel 8",
            "user-agent": "v2rayNG/1.9",
            "_request_ip": "203.0.113.9",
        },
    )

    row = UserDevice.query.filter_by(telegram_id=76, hwid="devA").first()
    assert row.device_os == "android"
    assert row.os_ver == "14"
    assert row.model == "Pixel 8"
    assert row.user_agent == "v2rayNG/1.9"
    assert row.request_ip == "203.0.113.9"


def test_devices_of_two_accounts_do_not_share_a_budget(app, db):
    _enable_user_device_limit(db, 1)

    assert user_device_gate(77, {"x-hwid": "shared-hwid"})[0] == "ok"
    assert user_device_gate(78, {"x-hwid": "shared-hwid"})[0] == "ok"
    assert count_user_devices(77) == 1
    assert count_user_devices(78) == 1


def test_subscription_device_settings_defaults(app, db):
    enabled, limit = subscription_device_settings()
    assert enabled is False and limit == 0


def test_subscription_device_settings_reads_values(app, db):
    db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
    db.session.add(SystemSetting(key="device_limit_per_user", value="4"))
    db.session.commit()
    enabled, limit = subscription_device_settings()
    assert enabled is True and limit == 4
