"""The device limit has to fire on the role that actually serves subscriptions.

Every test here builds the **sub** role's app. That is the whole point: the gate used to open
with `if is_sub(): return ("ok", {})`, so on the one role that serves configs it passed
everybody before counting anything, and a suite that only ever exercised a worker-shaped app
stayed green throughout. The second short-circuit was quieter still -- the budget was counted
through a join on `Client`, and the sub role holds no `Client` row for a client issued on a
node, so even with the early return gone the count would have been zero.

The clients here therefore have **no** `Client` row at all: that is what a node-issued key
looks like from the sub role's side.
"""

import pytest

from panel_core.xray import gateway as gw

from tests.schema import ensure_schema


@pytest.fixture
def sub_app(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "sub")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/sub.db"))
    monkeypatch.chdir(tmp_path)
    gw.set_xray_gateway(None)

    import importlib

    module = importlib.import_module("panel_core.roles.sub")
    return module.create_app()


def _enable_limit(sub_app, limit):
    from panel_core.extensions import db
    from panel_core.models import SystemSetting

    with sub_app.app_context():
        db.session.add(SystemSetting(key="device_limit_enabled", value="true"))
        db.session.add(SystemSetting(key="device_limit_per_user", value=str(limit)))
        db.session.commit()


def test_the_sub_role_enforces_the_limit_it_used_to_skip(sub_app):
    from panel_core.models import Client
    from panel_core.services.device_tracking import user_device_gate

    _enable_limit(sub_app, 1)

    with sub_app.app_context():
        assert Client.query.filter_by(telegram_id=500).count() == 0

        assert user_device_gate(500, {"x-hwid": "phone"})[0] == "ok"
        state, headers = user_device_gate(500, {"x-hwid": "laptop"})

    assert state == "limit"
    assert headers.get("x-hwid-max-devices-reached") == "true"


def test_the_sub_role_writes_the_ledger(sub_app):
    from panel_core.models import UserDevice
    from panel_core.services.device_tracking import user_device_gate

    _enable_limit(sub_app, 3)

    with sub_app.app_context():
        user_device_gate(501, {"x-hwid": "phone", "x-device-os": "ios"})

        rows = UserDevice.query.filter_by(telegram_id=501).all()
        assert [r.hwid for r in rows] == ["phone"]
        assert rows[0].device_os == "ios"


def test_the_page_and_the_gate_report_the_same_number(sub_app):
    """The counter and the enforcement are two call sites of one ledger.

    Fixing one and not the other is worse than leaving both broken: the user would be shown
    zero devices while the limit silently held, which reads as a broken counter.
    """
    from panel_core.api.subscription import _user_device_summary
    from panel_core.services.device_tracking import user_device_gate

    _enable_limit(sub_app, 5)

    with sub_app.app_context():
        user_device_gate(502, {"x-hwid": "phone"})
        user_device_gate(502, {"x-hwid": "laptop"})
        user_device_gate(502, {"x-hwid": "phone"})

        summary = _user_device_summary(502)

    assert summary == {"count": 2, "limit": 5}


def test_the_summary_is_absent_while_the_limit_is_off(sub_app):
    from panel_core.api.subscription import _user_device_summary

    with sub_app.app_context():
        assert _user_device_summary(503) is None


def test_the_subscription_route_gates_a_node_client_by_its_account(sub_app, monkeypatch):
    """End to end on the role: a config request for a key that exists only in a snapshot."""

    from panel_core.extensions import db
    from panel_core.models import LinkedPanel

    _enable_limit(sub_app, 1)

    snapshot = {
        "inbounds": [
            {
                "tag": "DE-vless",
                "port": 443,
                "protocol": "vless",
                "label": "Germany",
                "stream_settings": {"network": "tcp", "security": "reality", "realitySettings": {}},
                "clients": [
                    {
                        "id": "11111111-2222-3333-4444-555555555555",
                        "email": "tg600_DE-vless",
                        "enable": True,
                        "up": 0,
                        "down": 0,
                        "limit_bytes": 0,
                        "expiry_time": 0,
                        "telegram_id": 600,
                    }
                ],
            }
        ]
    }

    with sub_app.app_context():
        db.session.add(
            LinkedPanel(
                name="de",
                url="https://node1.example.com",
                federation_token="t",
                enable=True,
                created_at=0,
            )
        )
        db.session.commit()

    monkeypatch.setattr("panel_core.services.panel_proxy.get_panel_snapshot", lambda panel_id: snapshot)
    monkeypatch.setattr("panel_core.services.sub_cache.get", lambda kind, key: None)
    monkeypatch.setattr("panel_core.services.sub_cache.set", lambda kind, key, value: None)

    client = sub_app.test_client()
    url = "/api/sub/11111111-2222-3333-4444-555555555555"

    first = client.get(url, headers={"User-Agent": "v2rayNG", "x-hwid": "phone"})
    assert first.status_code == 200
    assert first.headers.get("x-hwid-active") == "true"

    second = client.get(url, headers={"User-Agent": "v2rayNG", "x-hwid": "laptop"})
    assert second.status_code == 200
    assert second.headers.get("x-hwid-max-devices-reached") == "true"
