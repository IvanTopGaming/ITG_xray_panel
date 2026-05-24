"""Admin-side tests for /api/bot/texts (JWT-protected)."""

import time
import pytest

import jwt as jwt_lib

from app.models import Admin, BotText
from app.utils import SECRET_KEY


@pytest.fixture
def app_with_admin(app):
    """Register bot_admin blueprint."""
    from app.api import bot_admin

    if not any(bp.name == "bot_admin" for bp in app.blueprints.values()):
        app.register_blueprint(bot_admin.bp, url_prefix="/api")
    return app


@pytest.fixture
def admin_headers(app_with_admin, db):
    """Mint a real JWT against SECRET_KEY (variant b — same as Tasks 1-5
    in test_api_bot_tariffs.py)."""
    admin = Admin(username="admin", password="x", password_changed_at=0)
    db.session.add(admin)
    db.session.commit()
    token = jwt_lib.encode(
        {
            "user": "admin",
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": 0,
            "exp": time.time() + 3600,
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(app_with_admin):
    return app_with_admin.test_client()


def test_get_texts_returns_grouped(app_with_admin, db, client, admin_headers):
    db.session.add(BotText(key="welcome.title", lang="ru", text="Привет"))
    db.session.add(BotText(key="welcome.title", lang="en", text="Hi"))
    db.session.add(BotText(key="menu.keys", lang="ru", text="Ключи"))
    db.session.commit()

    resp = client.get("/api/bot/texts", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    items = body["texts"]
    by_key_lang = {(t["key"], t["lang"]): t["text"] for t in items}
    assert by_key_lang[("welcome.title", "ru")] == "Привет"
    assert by_key_lang[("welcome.title", "en")] == "Hi"
    assert by_key_lang[("menu.keys", "ru")] == "Ключи"


def test_put_text_creates_new_row(app_with_admin, db, client, admin_headers):
    resp = client.put(
        "/api/bot/texts/welcome.title",
        headers=admin_headers,
        json={"lang": "en", "text": "Hello world"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["key"] == "welcome.title"
    assert body["lang"] == "en"
    assert body["text"] == "Hello world"
    assert BotText.query.get(("welcome.title", "en")).text == "Hello world"


def test_put_text_updates_existing(app_with_admin, db, client, admin_headers):
    db.session.add(BotText(key="welcome.title", lang="ru", text="старое"))
    db.session.commit()

    resp = client.put(
        "/api/bot/texts/welcome.title",
        headers=admin_headers,
        json={"lang": "ru", "text": "новое"},
    )
    assert resp.status_code == 200
    assert BotText.query.get(("welcome.title", "ru")).text == "новое"


def test_put_text_rejects_invalid_lang(app_with_admin, db, client, admin_headers):
    resp = client.put(
        "/api/bot/texts/welcome.title",
        headers=admin_headers,
        json={"lang": "xx", "text": "yo"},
    )
    assert resp.status_code == 400


def test_delete_text_removes_row(app_with_admin, db, client, admin_headers):
    db.session.add(BotText(key="welcome.title", lang="ru", text="Привет"))
    db.session.commit()

    resp = client.delete("/api/bot/texts/welcome.title?lang=ru", headers=admin_headers)
    assert resp.status_code == 200
    assert BotText.query.get(("welcome.title", "ru")) is None


def test_delete_text_404_if_missing(app_with_admin, db, client, admin_headers):
    resp = client.delete("/api/bot/texts/welcome.title?lang=ru", headers=admin_headers)
    assert resp.status_code == 404


def test_get_keys_metadata_returns_keys_and_descriptions(app_with_admin, db, client, admin_headers):
    """The /keys endpoint reads bot_texts_meta.yaml + bot_texts_defaults.yaml
    and returns one entry per key with {key, description, variables, default_ru, default_en}."""
    resp = client.get("/api/bot/texts/keys", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    items = body["keys"]
    # At minimum, the field shape must be correct. The defaults YAML is
    # currently empty (Task 6 fills it). Accept empty list pre-Task 6.
    assert isinstance(items, list)
    if items:
        first = items[0]
        assert "key" in first
        assert "description" in first
        assert "variables" in first
        assert "default_ru" in first
        assert "default_en" in first


def test_put_text_publishes_event(app_with_admin, db, client, admin_headers):
    """Saving a text must call bot_events.publish('texts_changed', ...)."""
    from unittest.mock import patch

    with patch("app.services.bot_events.publish") as mock_publish:
        client.put(
            "/api/bot/texts/welcome.title",
            headers=admin_headers,
            json={"lang": "ru", "text": "x"},
        )
    mock_publish.assert_called_once()
    args = mock_publish.call_args
    assert args.args[0] == "texts_changed"
