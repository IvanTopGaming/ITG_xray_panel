"""Tests for GET /api/bot/payments admin endpoint."""

import datetime as dt
import time

import jwt
import pytest

from app.extensions import db
from app.models import Admin, Payment, Tariff, TariffItem
from app.utils import SECRET_KEY


@pytest.fixture
def app(app):
    """Extend the base app fixture with bot_admin blueprint."""
    from app.api import bot_admin

    if not any(bp.name == "bot_admin" for bp in app.blueprints.values()):
        app.register_blueprint(bot_admin.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_token(app, db):
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
    return token


@pytest.fixture
def tariff(app):
    with app.app_context():
        t = Tariff(
            name="Standard",
            price_rub=150,
            period_days=30,
            visibility="public",
            enabled=True,
            is_trial=False,
        )
        t.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        db.session.add(t)
        db.session.commit()
        yield t.id


def _seed(app, tariff_id, status, tg_id=42, days_ago=0):
    with app.app_context():
        p = Payment(
            yookassa_id=f"yk-{status}-{tg_id}",
            telegram_id=tg_id,
            tariff_id=tariff_id,
            tariff_snapshot={"name": "x", "price_rub": 150, "period_days": 30, "items": []},
            amount_rub=150,
            status=status,
            metadata_json={},
        )
        db.session.add(p)
        db.session.flush()
        p.created_at = dt.datetime.utcnow() - dt.timedelta(days=days_ago)
        if status == "succeeded":
            p.paid_at = p.created_at
        db.session.commit()


def test_list_payments_returns_all_records(app, client, admin_token, tariff):
    _seed(app, tariff, "succeeded", tg_id=42)
    _seed(app, tariff, "pending", tg_id=43)
    resp = client.get("/api/bot/payments", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 2
    assert {p["status"] for p in body["items"]} == {"succeeded", "pending"}


def test_list_payments_filters_by_status(app, client, admin_token, tariff):
    _seed(app, tariff, "succeeded", tg_id=42)
    _seed(app, tariff, "pending", tg_id=43)
    resp = client.get(
        "/api/bot/payments?status=succeeded",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "succeeded"


def test_list_payments_filters_by_tg_id(app, client, admin_token, tariff):
    _seed(app, tariff, "succeeded", tg_id=42)
    _seed(app, tariff, "succeeded", tg_id=43)
    resp = client.get(
        "/api/bot/payments?telegram_id=42",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    body = resp.get_json()
    assert body["total"] == 1
    assert body["items"][0]["telegram_id"] == 42


def test_list_payments_stats_count_this_month(app, client, admin_token, tariff):
    _seed(app, tariff, "succeeded", tg_id=42)
    _seed(app, tariff, "succeeded", tg_id=43)
    _seed(app, tariff, "pending", tg_id=44)  # not counted
    resp = client.get("/api/bot/payments", headers={"Authorization": f"Bearer {admin_token}"})
    body = resp.get_json()
    assert body["stats"]["month_count"] == 2
    assert body["stats"]["month_amount_rub"] == 300


def test_list_payments_requires_auth(client):
    resp = client.get("/api/bot/payments")
    assert resp.status_code == 401
