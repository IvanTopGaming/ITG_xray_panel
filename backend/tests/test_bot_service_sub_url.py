import pytest

from panel_core.extensions import db
from panel_core.models import TelegramUser


@pytest.fixture
def app(app):
    from panel_core.api import bot_service

    if "bot_service" not in app.blueprints:
        app.register_blueprint(bot_service.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_new_user_gets_sub_token(app):
    with app.app_context():
        u = TelegramUser(telegram_id=500)
        db.session.add(u)
        db.session.commit()
        fetched = db.session.get(TelegramUser, 500)
        assert fetched.sub_token
        assert len(fetched.sub_token) == 36


def test_get_user_state_includes_sub_url(client, app, monkeypatch):
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.com")
    with app.app_context():
        from panel_core.models import SystemSetting

        db.session.add(SystemSetting(key="bot_service_token", value="testtoken"))
        u = TelegramUser(telegram_id=900, sub_token="tok-900-bbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        db.session.add(u)
        db.session.commit()

    resp = client.get(
        "/api/bot-service/users/900/state",
        headers={"Authorization": "Bearer testtoken"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sub_url"] == "https://sub.example.com/api/sub/u/tok-900-bbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_get_user_state_no_sub_domain_uses_panel_fallback(client, app, monkeypatch):

    monkeypatch.delenv("SUB_DOMAIN", raising=False)
    monkeypatch.setenv("PANEL_DOMAIN", "panel.example.com")
    monkeypatch.setenv("PANEL_SECRET_PATH", "secret123")
    with app.app_context():
        from panel_core.models import SystemSetting

        db.session.add(SystemSetting(key="bot_service_token", value="testtoken2"))
        u = TelegramUser(telegram_id=901, sub_token="tok-901-cccccccccccccccccccccccccccc")
        db.session.add(u)
        db.session.commit()
    resp = client.get(
        "/api/bot-service/users/901/state",
        headers={"Authorization": "Bearer testtoken2"},
    )
    assert (
        resp.get_json()["sub_url"]
        == "https://panel.example.com/secret123/api/sub/u/tok-901-cccccccccccccccccccccccccccc"
    )


def test_get_user_state_no_domains_returns_none(client, app, monkeypatch):
    monkeypatch.delenv("SUB_DOMAIN", raising=False)
    monkeypatch.delenv("PANEL_DOMAIN", raising=False)
    with app.app_context():
        from panel_core.models import SystemSetting

        db.session.add(SystemSetting(key="bot_service_token", value="testtoken3"))
        u = TelegramUser(telegram_id=902, sub_token="tok-902-dddddddddddddddddddddddddddd")
        db.session.add(u)
        db.session.commit()
    resp = client.get(
        "/api/bot-service/users/902/state",
        headers={"Authorization": "Bearer testtoken3"},
    )
    assert resp.get_json()["sub_url"] is None
