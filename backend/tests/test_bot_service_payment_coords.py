"""Tests for POST /bot-service/payments/<id>/chat-coords."""

import pytest

from app.extensions import db as _db
from app.models import Payment, SystemSetting


@pytest.fixture
def app_with_bot_service(app):
    from app.api import bot_service

    if not any(bp.name == "bot_service" for bp in app.blueprints.values()):
        app.register_blueprint(bot_service.bp, url_prefix="/api")
    _db.session.add(SystemSetting(key="bot_service_token", value="svc-token"))
    _db.session.commit()
    return app


@pytest.fixture
def svc_headers():
    return {"Authorization": "Bearer svc-token"}


@pytest.fixture
def http(app_with_bot_service):
    return app_with_bot_service.test_client()


def _make_payment(db, *, status="pending"):
    p = Payment(
        yookassa_id=f"yk-{status}-1",
        telegram_id=42,
        tariff_id=1,
        tariff_snapshot={},
        amount_rub=199,
        status=status,
    )
    db.session.add(p)
    db.session.commit()
    return p


def test_set_chat_coords_persists(http, svc_headers, db):
    p = _make_payment(db)
    resp = http.post(
        f"/api/bot-service/payments/{p.id}/chat-coords",
        json={"chat_id": 8070297806, "message_id": 12345},
        headers=svc_headers,
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["chat_id"] == 8070297806
    assert body["message_id"] == 12345

    fetched = db.session.get(Payment, p.id)
    assert fetched.chat_id == 8070297806
    assert fetched.message_id == 12345


def test_set_chat_coords_overwrites(http, svc_headers, db):
    p = _make_payment(db)
    http.post(
        f"/api/bot-service/payments/{p.id}/chat-coords",
        json={"chat_id": 1, "message_id": 1},
        headers=svc_headers,
    )
    resp = http.post(
        f"/api/bot-service/payments/{p.id}/chat-coords",
        json={"chat_id": 2, "message_id": 2},
        headers=svc_headers,
    )
    assert resp.status_code == 200
    fetched = db.session.get(Payment, p.id)
    assert fetched.chat_id == 2
    assert fetched.message_id == 2


def test_set_chat_coords_404_for_unknown_payment(http, svc_headers, db):
    resp = http.post(
        "/api/bot-service/payments/99999/chat-coords",
        json={"chat_id": 1, "message_id": 1},
        headers=svc_headers,
    )
    assert resp.status_code == 404


def test_set_chat_coords_400_on_bad_body(http, svc_headers, db):
    p = _make_payment(db)
    resp = http.post(
        f"/api/bot-service/payments/{p.id}/chat-coords",
        json={"chat_id": "not-int"},
        headers=svc_headers,
    )
    assert resp.status_code == 400


def test_set_chat_coords_requires_token(http, db):
    p = _make_payment(db)
    resp = http.post(
        f"/api/bot-service/payments/{p.id}/chat-coords",
        json={"chat_id": 1, "message_id": 1},
    )
    assert resp.status_code == 401
