import pytest

from panel_core.extensions import db as _db
from panel_core.models import SystemSetting, TelegramUser


@pytest.fixture
def app_with_bot_service(app):
    from panel_core.api import bot_service

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


def test_upsert_response_includes_language_chosen_false(http, svc_headers, db):
    resp = http.post(
        "/api/bot-service/users",
        json={"telegram_id": 501, "username": "alpha", "language_code": "ru"},
        headers=svc_headers,
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["language_chosen"] is False


def test_upsert_response_preserves_language_chosen_true(http, svc_headers, db):
    db.session.add(TelegramUser(telegram_id=502, language="en", language_chosen=True))
    db.session.commit()
    resp = http.post(
        "/api/bot-service/users",
        json={"telegram_id": 502, "username": "beta", "language_code": "en"},
        headers=svc_headers,
    )
    assert resp.status_code == 200
    assert resp.get_json()["language_chosen"] is True


def test_get_state_includes_language_chosen(http, svc_headers, db):
    db.session.add(TelegramUser(telegram_id=503, language="ru", language_chosen=True))
    db.session.commit()
    resp = http.get("/api/bot-service/users/503/state", headers=svc_headers)
    assert resp.status_code == 200
    assert resp.get_json()["language_chosen"] is True


def test_get_state_language_chosen_false_for_unknown_user(http, svc_headers, db):
    resp = http.get("/api/bot-service/users/9999/state", headers=svc_headers)
    assert resp.status_code == 200
    assert resp.get_json()["language_chosen"] is False


from unittest.mock import patch


def test_set_language_persists_choice(http, svc_headers, db):
    db.session.add(TelegramUser(telegram_id=601, language="ru", language_chosen=False))
    db.session.commit()
    with patch("panel_core.api.bot_service.bot_events.publish") as pub:
        resp = http.post(
            "/api/bot-service/users/601/language",
            json={"language": "en"},
            headers=svc_headers,
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["language"] == "en"
    assert body["language_chosen"] is True

    fetched = db.session.get(TelegramUser, 601)
    assert fetched.language == "en"
    assert fetched.language_chosen is True

    pub.assert_called_once_with("user_language_changed", telegram_id=601, payload={"language": "en"})


def test_set_language_rejects_invalid_lang(http, svc_headers, db):
    db.session.add(TelegramUser(telegram_id=602, language="ru"))
    db.session.commit()
    resp = http.post(
        "/api/bot-service/users/602/language",
        json={"language": "fr"},
        headers=svc_headers,
    )
    assert resp.status_code == 400


def test_set_language_404_for_unknown_user(http, svc_headers, db):
    resp = http.post(
        "/api/bot-service/users/77777/language",
        json={"language": "ru"},
        headers=svc_headers,
    )
    assert resp.status_code == 404


def test_set_language_requires_token(http, db):
    db.session.add(TelegramUser(telegram_id=603, language="ru"))
    db.session.commit()
    resp = http.post(
        "/api/bot-service/users/603/language",
        json={"language": "ru"},
    )
    assert resp.status_code == 401
