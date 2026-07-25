import time
from unittest.mock import patch

import jwt
import pytest

from panel_core.models import Admin, Tariff, TariffItem
from panel_core.utils import SECRET_KEY


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


def test_list_tariffs_empty(app_with_bot_api, db, client, auth_headers):
    resp = client.get("/api/bot/tariffs", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.get_json() == {"tariffs": []}


def test_list_tariffs_returns_with_items(app_with_bot_api, db, client, auth_headers):
    t = Tariff(name="Standard", price_rub=150, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add(
        TariffItem(
            tariff_id=t.id,
            inbound_tag="DE-vless",
            label="Germany",
            traffic_gb=0,
            sort_order=0,
        )
    )
    db.session.add(
        TariffItem(
            tariff_id=t.id,
            inbound_tag="MSK-vless",
            label="Russia",
            traffic_gb=70,
            sort_order=1,
        )
    )
    db.session.commit()

    resp = client.get("/api/bot/tariffs", headers=auth_headers)
    assert resp.status_code == 200
    payload = resp.get_json()
    assert len(payload["tariffs"]) == 1
    tariff = payload["tariffs"][0]
    assert tariff["name"] == "Standard"
    assert tariff["price_rub"] == 150
    assert tariff["period_days"] == 30
    assert tariff["visibility"] == "public"
    assert tariff["is_trial"] is False
    assert tariff["enabled"] is True
    assert len(tariff["items"]) == 2
    assert tariff["items"][0]["inbound_tag"] == "DE-vless"
    assert tariff["items"][0]["traffic_gb"] == 0
    assert tariff["items"][1]["inbound_tag"] == "MSK-vless"
    assert tariff["items"][1]["traffic_gb"] == 70


def test_list_tariffs_requires_auth(app_with_bot_api, db, client):
    resp = client.get("/api/bot/tariffs")
    assert resp.status_code == 401


def test_create_tariff_minimal(app_with_bot_api, db, client, auth_headers):
    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={"name": "Basic", "price_rub": 100, "period_days": 30, "items": []},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["name"] == "Basic"
    assert body["visibility"] == "public"
    assert body["enabled"] is True
    assert body["items"] == []
    assert body["id"] > 0


def test_create_tariff_with_items(app_with_bot_api, db, client, auth_headers):
    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={
            "name": "Standard",
            "price_rub": 150,
            "period_days": 30,
            "visibility": "public",
            "enabled": True,
            "sort_order": 5,
            "items": [
                {
                    "inbound_tag": "DE-vless",
                    "label": "Germany",
                    "traffic_gb": 0,
                    "sort_order": 0,
                },
                {
                    "inbound_tag": "MSK-vless",
                    "label": "Russia",
                    "traffic_gb": 70,
                    "sort_order": 1,
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert len(body["items"]) == 2
    assert body["items"][0]["inbound_tag"] == "DE-vless"
    assert body["items"][1]["inbound_tag"] == "MSK-vless"


def test_create_tariff_rejects_missing_required(app_with_bot_api, db, client, auth_headers):
    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={"name": "NoPrice", "period_days": 30, "items": []},
    )
    assert resp.status_code == 400


def test_create_tariff_rejects_negative_period(app_with_bot_api, db, client, auth_headers):
    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={"name": "X", "price_rub": 100, "period_days": 0, "items": []},
    )
    assert resp.status_code == 400


def test_create_tariff_rejects_invalid_visibility(app_with_bot_api, db, client, auth_headers):
    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={"name": "X", "price_rub": 100, "period_days": 30, "visibility": "hidden", "items": []},
    )
    assert resp.status_code == 400


def test_create_tariff_rejects_duplicate_item_inbound(app_with_bot_api, db, client, auth_headers):
    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={
            "name": "Bad",
            "price_rub": 100,
            "period_days": 30,
            "items": [
                {"inbound_tag": "X", "traffic_gb": 10, "sort_order": 0},
                {"inbound_tag": "X", "traffic_gb": 20, "sort_order": 1},
            ],
        },
    )
    assert resp.status_code == 400
    assert "inbound_tag" in resp.get_data(as_text=True).lower()


def test_create_trial_when_none_exists(app_with_bot_api, db, client, auth_headers):
    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={"name": "Trial", "price_rub": 0, "period_days": 1, "is_trial": True, "items": []},
    )
    assert resp.status_code == 201


def test_create_trial_rejected_when_one_exists(app_with_bot_api, db, client, auth_headers):
    from panel_core.models import Tariff

    db.session.add(Tariff(name="ExistingTrial", price_rub=0, period_days=1, is_trial=True))
    db.session.commit()

    resp = client.post(
        "/api/bot/tariffs",
        headers=auth_headers,
        json={"name": "AnotherTrial", "price_rub": 0, "period_days": 1, "is_trial": True, "items": []},
    )
    assert resp.status_code == 400
    assert "trial" in resp.get_data(as_text=True).lower()


def test_update_tariff_changes_fields(app_with_bot_api, db, client, auth_headers):
    t = Tariff(name="Old", price_rub=100, period_days=30)
    db.session.add(t)
    db.session.commit()

    resp = client.put(
        f"/api/bot/tariffs/{t.id}",
        headers=auth_headers,
        json={"name": "New", "price_rub": 200, "period_days": 60, "visibility": "private", "items": []},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["name"] == "New"
    assert body["price_rub"] == 200
    assert body["visibility"] == "private"


def test_update_tariff_replaces_items(app_with_bot_api, db, client, auth_headers):
    t = Tariff(name="X", price_rub=100, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="OLD", traffic_gb=10))
    db.session.commit()

    resp = client.put(
        f"/api/bot/tariffs/{t.id}",
        headers=auth_headers,
        json={
            "name": "X",
            "price_rub": 100,
            "period_days": 30,
            "items": [
                {"inbound_tag": "NEW1", "traffic_gb": 5, "sort_order": 0},
                {"inbound_tag": "NEW2", "traffic_gb": 0, "sort_order": 1},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    tags = {item["inbound_tag"] for item in body["items"]}
    assert tags == {"NEW1", "NEW2"}
    db.session.expire_all()
    fresh = db.session.get(Tariff, t.id)
    assert {i.inbound_tag for i in fresh.items} == {"NEW1", "NEW2"}


def test_update_nonexistent_tariff_returns_404(app_with_bot_api, db, client, auth_headers):
    resp = client.put(
        "/api/bot/tariffs/9999",
        headers=auth_headers,
        json={"name": "X", "price_rub": 100, "period_days": 30, "items": []},
    )
    assert resp.status_code == 404


def test_update_existing_trial_keeps_trial_flag(app_with_bot_api, db, client, auth_headers):

    t = Tariff(name="Trial", price_rub=0, period_days=1, is_trial=True)
    db.session.add(t)
    db.session.commit()

    resp = client.put(
        f"/api/bot/tariffs/{t.id}",
        headers=auth_headers,
        json={"name": "Trial v2", "price_rub": 0, "period_days": 1, "is_trial": True, "items": []},
    )
    assert resp.status_code == 200


def test_update_to_trial_rejected_if_other_trial_exists(app_with_bot_api, db, client, auth_headers):
    db.session.add(Tariff(name="Trial", price_rub=0, period_days=1, is_trial=True))
    other = Tariff(name="Standard", price_rub=100, period_days=30)
    db.session.add(other)
    db.session.commit()

    resp = client.put(
        f"/api/bot/tariffs/{other.id}",
        headers=auth_headers,
        json={"name": "Standard", "price_rub": 100, "period_days": 30, "is_trial": True, "items": []},
    )
    assert resp.status_code == 400


def test_delete_tariff_soft_archives_it(app_with_bot_api, db, client, auth_headers):
    t = Tariff(name="ToArchive", price_rub=100, period_days=30, visibility="public")
    db.session.add(t)
    db.session.commit()
    tid = t.id

    resp = client.delete(f"/api/bot/tariffs/{tid}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["visibility"] == "archived"

    db.session.expire_all()
    assert db.session.get(Tariff, tid) is not None
    assert db.session.get(Tariff, tid).visibility == "archived"


def test_delete_already_archived_is_idempotent(app_with_bot_api, db, client, auth_headers):
    t = Tariff(name="X", price_rub=100, period_days=30, visibility="archived")
    db.session.add(t)
    db.session.commit()
    resp = client.delete(f"/api/bot/tariffs/{t.id}", headers=auth_headers)
    assert resp.status_code == 200


def test_delete_nonexistent_returns_404(app_with_bot_api, db, client, auth_headers):
    resp = client.delete("/api/bot/tariffs/9999", headers=auth_headers)
    assert resp.status_code == 404


def test_duplicate_tariff_copies_base_and_items(app_with_bot_api, db, client, auth_headers):
    t = Tariff(
        name="Standard",
        price_rub=150,
        period_days=30,
        visibility="private",
        sort_order=3,
    )
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="DE", traffic_gb=0, sort_order=0))
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="RU", traffic_gb=70, sort_order=1))
    db.session.commit()

    resp = client.post(f"/api/bot/tariffs/{t.id}/duplicate", headers=auth_headers)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["id"] != t.id
    assert body["name"] == "Standard (копия)"
    assert body["price_rub"] == 150
    assert body["period_days"] == 30
    assert body["visibility"] == "public"
    assert body["enabled"] is True
    assert body["is_trial"] is False
    item_tags = {i["inbound_tag"] for i in body["items"]}
    assert item_tags == {"DE", "RU"}


def test_duplicate_trial_does_not_propagate_trial_flag(app_with_bot_api, db, client, auth_headers):
    t = Tariff(name="Trial", price_rub=0, period_days=1, is_trial=True)
    db.session.add(t)
    db.session.commit()
    resp = client.post(f"/api/bot/tariffs/{t.id}/duplicate", headers=auth_headers)
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["is_trial"] is False


def test_duplicate_nonexistent_returns_404(app_with_bot_api, db, client, auth_headers):
    resp = client.post("/api/bot/tariffs/9999/duplicate", headers=auth_headers)
    assert resp.status_code == 404


def test_update_tariff_returns_backfill_summary_and_backfills_local_holder(app_with_bot_api, db, client, auth_headers):
    import uuid

    from panel_core.models import Client, Inbound

    db.session.add_all(
        [
            Inbound(tag="DE-vless", protocol="vless", port=20001, stream_settings="{}"),
            Inbound(tag="MSK-vless", protocol="vless", port=20002, stream_settings="{}"),
        ]
    )
    db.session.flush()
    t = Tariff(name="Standard", price_rub=150, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="DE-vless", traffic_gb=0, sort_order=0))
    db.session.commit()

    now = int(time.time() * 1000)
    expiry = now + 10 * 86400_000
    db.session.add(
        Client(
            id=str(uuid.uuid4()),
            email="holder_DE",
            inbound_tag="DE-vless",
            telegram_id=42,
            tariff_id=t.id,
            limit_bytes=0,
            expiry_time=expiry,
            enable=True,
        )
    )
    db.session.commit()

    payload = {
        "name": "Standard",
        "price_rub": 150,
        "period_days": 30,
        "items": [
            {"inbound_tag": "DE-vless", "traffic_gb": 0},
            {"inbound_tag": "MSK-vless", "traffic_gb": 70},
        ],
    }
    with patch("panel_core.services.provisioning._sync_after_provision"):
        resp = client.put(f"/api/bot/tariffs/{t.id}", json=payload, headers=auth_headers)

    assert resp.status_code == 200
    body = resp.get_json()
    assert "backfill" in body
    assert body["backfill"]["created_local"] == 1
    assert body["backfill"]["panels_unreachable"] == []

    new = Client.query.filter_by(telegram_id=42, inbound_tag="MSK-vless").first()
    assert new is not None
    assert new.expiry_time == expiry
    assert new.limit_bytes == 70 * (1024**3)
