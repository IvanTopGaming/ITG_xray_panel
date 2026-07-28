import json
import logging
import time
from unittest.mock import patch

import jwt
import pytest

from panel_core.models import Admin, Inbound, Tariff, TariffItem
from panel_core.utils import SECRET_KEY
from panel_core.xray import gateway as gw
from panel_core.xray.local import LocalXrayGateway


@pytest.fixture
def app_with_bot_api(app):

    from panel_core.api import bot_admin

    if not any(bp.name == "bot_admin" for bp in app.blueprints.values()):
        app.register_blueprint(bot_admin.bp, url_prefix="/api")
    return app


@pytest.fixture
def auth_headers(app_with_bot_api, db):

    pwd_version = int(time.time())
    admin = Admin(
        username="admin",
        password="hashed-not-checked-by-token-required",
        password_changed_at=pwd_version,
    )
    db.session.add(admin)
    db.session.commit()

    token = jwt.encode(
        {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": pwd_version,
            "exp": int(time.time()) + 3600,
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(app_with_bot_api):
    return app_with_bot_api.test_client()


def _seed_legacy_tariff(db, *, name="Legacy", tag="legacy-tag", protocol="vless", port=31001):
    db.session.add(
        Inbound(
            tag=tag,
            port=port,
            protocol=protocol,
            stream_settings=json.dumps({"network": "tcp", "security": "none"}),
        )
    )
    tariff = Tariff(name=name, price_rub=100, period_days=30)
    db.session.add(tariff)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag=tag, traffic_gb=10, panel_id=None))
    db.session.commit()
    return tariff


def test_create_tariff_rejects_item_without_panel_id(app_with_bot_api, db, client, auth_headers):
    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={
            "name": "Standard",
            "price_rub": 150,
            "period_days": 30,
            "items": [{"inbound_tag": "DE-vless", "traffic_gb": 0, "sort_order": 0}],
        },
    )
    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_data(as_text=True)
    assert "panel_id" in body
    assert "DE-vless" in body
    assert Tariff.query.count() == 0


def test_create_tariff_rejects_null_panel_id(app_with_bot_api, db, client, auth_headers):
    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={
            "name": "Standard",
            "price_rub": 150,
            "period_days": 30,
            "items": [{"inbound_tag": "DE-vless", "traffic_gb": 0, "panel_id": None}],
        },
    )
    assert resp.status_code == 400
    assert "panel_id" in resp.get_data(as_text=True)


def test_create_tariff_accepts_item_with_panel_id(app_with_bot_api, db, client, auth_headers):
    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={
            "name": "Standard",
            "price_rub": 150,
            "period_days": 30,
            "items": [{"inbound_tag": "DE-vless", "traffic_gb": 0, "panel_id": 7, "sort_order": 0}],
        },
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["items"][0]["panel_id"] == 7


def test_create_tariff_without_items_is_still_allowed(app_with_bot_api, db, client, auth_headers):
    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={"name": "Empty", "price_rub": 100, "period_days": 30, "items": []},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)


def test_update_tariff_rejects_item_without_panel_id(app_with_bot_api, db, client, auth_headers):
    t = Tariff(name="X", price_rub=100, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="OLD", traffic_gb=10, panel_id=3))
    db.session.commit()

    resp = client.put(
        f"/api/bot/tariffs/{t.id}",
        headers=auth_headers,
        json={
            "name": "X",
            "price_rub": 100,
            "period_days": 30,
            "items": [{"inbound_tag": "NEW", "traffic_gb": 5}],
        },
    )
    assert resp.status_code == 400
    assert "panel_id" in resp.get_data(as_text=True)

    db.session.expire_all()
    fresh = db.session.get(Tariff, t.id)
    assert [(i.inbound_tag, i.panel_id) for i in fresh.items] == [("OLD", 3)]


def test_update_tariff_accepts_items_with_panel_id(app_with_bot_api, db, client, auth_headers):
    t = Tariff(name="X", price_rub=100, period_days=30)
    db.session.add(t)
    db.session.commit()

    with patch("panel_core.services.provisioning._sync_after_provision"):
        resp = client.put(
            f"/api/bot/tariffs/{t.id}",
            headers=auth_headers,
            json={
                "name": "X",
                "price_rub": 100,
                "period_days": 30,
                "items": [{"inbound_tag": "NEW", "traffic_gb": 5, "panel_id": 4}],
            },
        )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["items"][0]["panel_id"] == 4


def test_duplicate_of_a_legacy_tariff_is_rejected(app_with_bot_api, db, client, auth_headers):
    tariff = _seed_legacy_tariff(db, name="Legacy Local", tag="legacy-dup", port=31002)

    resp = client.post(f"/api/bot/tariffs/{tariff.id}/duplicate", headers=auth_headers)
    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_data(as_text=True)
    assert "panel_id" in body
    assert "legacy-dup" in body
    assert Tariff.query.count() == 1


def test_provisioning_error_names_the_tariff_and_the_inbound(app, db):
    from panel_core.models import Client
    from panel_core.services import provisioning

    tariff = _seed_legacy_tariff(db, name="Legacy Local", tag="legacy-prov", port=31003)
    gw.set_xray_gateway(gw.RemoteXrayGateway())

    with pytest.raises(gw.LocalXrayUnavailable) as excinfo:
        provisioning.apply_tariff_for_user(9001, tariff, source="test", operation_id="test-op")

    message = str(excinfo.value)
    assert "Legacy Local" in message
    assert "legacy-prov" in message
    assert "panel_id" in message

    db.session.rollback()
    assert Client.query.filter_by(telegram_id=9001).count() == 0


def test_provisioning_still_works_on_a_gateway_with_local_xray(app, db):
    from panel_core.models import Client
    from panel_core.services import provisioning

    tariff = _seed_legacy_tariff(db, name="Legacy Local", tag="legacy-worker", protocol="trojan", port=31004)

    calls = []

    class _Recording(LocalXrayGateway):
        def apply_config(self, validate=True):
            calls.append("apply_config")

        def restart(self):
            calls.append("restart")

    gw.set_xray_gateway(_Recording())

    provisioning.apply_tariff_for_user(9002, tariff, source="test", operation_id="test-op")

    assert Client.query.filter_by(telegram_id=9002).count() == 1
    assert calls == ["apply_config", "restart"]


def test_startup_audit_warns_and_names_the_tariff(app, db, caplog):
    from panel_core.app_base import audit_tariff_items_without_panel_id

    _seed_legacy_tariff(db, name="Legacy Local", tag="legacy-audit", port=31005)
    gw.set_xray_gateway(gw.RemoteXrayGateway())

    with caplog.at_level(logging.WARNING):
        audit_tariff_items_without_panel_id(app)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "expected a warning about tariff items without panel_id"
    joined = " ".join(warnings)
    assert "Legacy Local" in joined
    assert "legacy-audit" in joined
    assert "panel_id" in joined


def test_startup_audit_reports_without_changing_anything(app, db, caplog):
    from panel_core.app_base import audit_tariff_items_without_panel_id

    tariff = _seed_legacy_tariff(db, name="Legacy Local", tag="legacy-untouched", port=31006)
    gw.set_xray_gateway(gw.RemoteXrayGateway())

    with caplog.at_level(logging.WARNING):
        audit_tariff_items_without_panel_id(app)

    db.session.expire_all()
    fresh = db.session.get(Tariff, tariff.id)
    assert [i.panel_id for i in fresh.items] == [None]
    assert TariffItem.query.count() == 1


def test_startup_audit_is_silent_when_every_item_has_a_panel_id(app, db, caplog):
    from panel_core.app_base import audit_tariff_items_without_panel_id

    t = Tariff(name="Remote Only", price_rub=100, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="remote-tag", traffic_gb=10, panel_id=2))
    db.session.commit()
    gw.set_xray_gateway(gw.RemoteXrayGateway())

    with caplog.at_level(logging.WARNING):
        audit_tariff_items_without_panel_id(app)

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_startup_audit_is_silent_on_a_role_with_local_xray(app, db, caplog):
    from panel_core.app_base import audit_tariff_items_without_panel_id

    _seed_legacy_tariff(db, name="Legacy Local", tag="legacy-on-worker", port=31007)
    gw.set_xray_gateway(LocalXrayGateway())

    with caplog.at_level(logging.WARNING):
        audit_tariff_items_without_panel_id(app)

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
