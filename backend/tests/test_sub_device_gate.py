import contextlib
import uuid

from app.models import Client, Inbound, SystemSetting
from app.services import device_tracking
from app.services.device_tracking import device_gate, user_device_gate


def _make_inbound(db, *, tag="DE-vless", device_limit=1, port=10001):
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


class _DbSessionTripwire:
    def __getattr__(self, name):
        raise AssertionError(f"db.session.{name} was called on the sub service")


@contextlib.contextmanager
def _tripwire_db_session():
    original = device_tracking.db.session
    device_tracking.db.session = _DbSessionTripwire()
    try:
        yield
    finally:
        device_tracking.db.session = original


def test_device_gate_skips_db_in_sub_mode(app, db, monkeypatch):
    monkeypatch.setenv("PANEL_ROLE", "sub")
    inbound = _make_inbound(db, device_limit=1)
    client = _make_client(db)
    db.session.commit()

    with _tripwire_db_session():
        state, headers = device_gate(client, inbound, {"x-hwid": "hwid-1"})

    assert state == "ok"
    assert headers == {}


def test_user_device_gate_skips_db_in_sub_mode(app, db, monkeypatch):
    monkeypatch.setenv("PANEL_ROLE", "sub")
    _make_inbound(db, device_limit=1)
    _make_client(db, telegram_id=99)
    db.session.commit()

    with _tripwire_db_session():
        state, headers = user_device_gate(99, {"x-hwid": "hwid-1"})

    assert state == "ok"
    assert headers == {}


def test_device_gate_master_mode_still_hits_db(app, db, monkeypatch):
    monkeypatch.delenv("PANEL_ROLE", raising=False)
    inbound = _make_inbound(db, device_limit=1)
    client = _make_client(db)
    db.session.commit()

    state, headers = device_gate(client, inbound, {"x-hwid": "hwid-1"})

    assert state == "ok"
    assert headers.get("x-hwid-active") == "true"
    from app.models import ClientDevice

    assert ClientDevice.query.filter_by(client_id=client.id, hwid="hwid-1").first() is not None


def test_user_device_gate_master_mode_still_hits_db(app, db, monkeypatch):
    monkeypatch.delenv("PANEL_ROLE", raising=False)
    _make_inbound(db, device_limit=1)
    _make_client(db, telegram_id=98)
    db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
    db.session.add(SystemSetting(key="device_limit_per_user", value="1"))
    db.session.commit()

    state, headers = user_device_gate(98, {"x-hwid": "hwid-1"})

    assert state == "ok"
    assert headers.get("x-hwid-active") == "true"
    from app.models import ClientDevice

    assert ClientDevice.query.filter_by(hwid="hwid-1").first() is not None
