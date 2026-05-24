"""Tests for /api/bot-service/* endpoints (bot-facing, service-token auth)."""

import pytest

from app.models import BotText, SystemSetting, TelegramUser


@pytest.fixture
def app_with_service_api(app, db):
    from app.api import bot_service

    if not any(bp.name == "bot_service" for bp in app.blueprints.values()):
        app.register_blueprint(bot_service.bp, url_prefix="/api")
    db.session.add(SystemSetting(key="bot_service_token", value="test-token"))
    db.session.commit()
    return app


@pytest.fixture
def service_headers():
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(app_with_service_api):
    return app_with_service_api.test_client()


def test_get_texts_empty(app_with_service_api, db, client, service_headers):
    resp = client.get("/api/bot-service/texts?lang=ru", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"version": 0, "texts": {}}


def test_get_texts_returns_only_requested_lang(app_with_service_api, db, client, service_headers):
    db.session.add(BotText(key="welcome.title", lang="ru", text="Привет"))
    db.session.add(BotText(key="welcome.title", lang="en", text="Hi"))
    db.session.add(BotText(key="menu.keys", lang="ru", text="Ключи"))
    db.session.commit()

    resp = client.get("/api/bot-service/texts?lang=ru", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body["texts"].keys()) == {"welcome.title", "menu.keys"}
    assert body["texts"]["welcome.title"] == "Привет"
    assert body["version"] > 0  # epoch from latest updated_at


def test_get_texts_requires_token(app_with_service_api, db, client):
    resp = client.get("/api/bot-service/texts?lang=ru")
    assert resp.status_code == 401


def test_get_texts_rejects_invalid_lang(app_with_service_api, db, client, service_headers):
    resp = client.get("/api/bot-service/texts?lang=xx", headers=service_headers)
    assert resp.status_code == 400


def test_upsert_user_creates_new(app_with_service_api, db, client, service_headers):
    resp = client.post(
        "/api/bot-service/users",
        headers=service_headers,
        json={"telegram_id": 999, "username": "ivan", "language_code": "ru"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["telegram_id"] == 999
    assert body["username"] == "ivan"
    assert body["language"] == "ru"


def test_upsert_user_updates_existing(app_with_service_api, db, client, service_headers):
    db.session.add(
        TelegramUser(
            telegram_id=42,
            username="old",
            language="en",
        )
    )
    db.session.commit()

    resp = client.post(
        "/api/bot-service/users",
        headers=service_headers,
        json={"telegram_id": 42, "username": "new", "language_code": "ru"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    # username refreshed
    assert body["username"] == "new"
    # but the existing language preference is preserved (NOT overwritten by language_code)
    assert body["language"] == "en"


def test_upsert_user_defaults_language_for_unknown_code(app_with_service_api, db, client, service_headers):
    """Telegram language_code 'fr' is not in our supported set — fallback to 'ru'."""
    resp = client.post(
        "/api/bot-service/users",
        headers=service_headers,
        json={"telegram_id": 100, "username": "alice", "language_code": "fr"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["language"] == "ru"


def test_upsert_user_normalizes_ru_dialects(app_with_service_api, db, client, service_headers):
    """language_code 'ru-RU' and 'ru' both map to 'ru'."""
    resp = client.post(
        "/api/bot-service/users",
        headers=service_headers,
        json={"telegram_id": 200, "username": "x", "language_code": "ru-RU"},
    )
    body = resp.get_json()
    assert body["language"] == "ru"


def test_upsert_user_rejects_missing_telegram_id(app_with_service_api, db, client, service_headers):
    resp = client.post(
        "/api/bot-service/users",
        headers=service_headers,
        json={"username": "x", "language_code": "ru"},
    )
    assert resp.status_code == 400


def test_upsert_user_requires_token(app_with_service_api, db, client):
    resp = client.post(
        "/api/bot-service/users",
        json={"telegram_id": 1, "language_code": "ru"},
    )
    assert resp.status_code == 401


def test_get_user_state_brand_new_user(app_with_service_api, db, client, service_headers):
    """Unknown telegram_id → trial available, no clients."""
    resp = client.get("/api/bot-service/users/9999/state", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["telegram_id"] == 9999
    assert body["trial_available"] is True
    assert body["clients"] == []
    assert body["expires_at_ms"] is None


def test_get_user_state_trial_used_no_active(app_with_service_api, db, client, service_headers):
    from datetime import datetime

    db.session.add(
        TelegramUser(
            telegram_id=42,
            language="ru",
            trial_used_at=datetime.utcnow(),
        )
    )
    db.session.commit()

    resp = client.get("/api/bot-service/users/42/state", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["trial_available"] is False
    assert body["clients"] == []


def test_get_user_state_with_active_clients(app_with_service_api, db, client, service_headers):
    import time

    from app.models import Client, Inbound

    inbound = Inbound(tag="DE-vless", protocol="vless", port=10001, stream_settings="{}")
    db.session.add(inbound)
    db.session.flush()
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    expiry = int(time.time() * 1000) + 86400_000 * 7
    db.session.add(
        Client(
            id="abc-1",
            email="tg42_DE-vless",
            inbound_tag="DE-vless",
            telegram_id=42,
            limit_bytes=0,
            expiry_time=expiry,
            enable=True,
        )
    )
    db.session.commit()

    resp = client.get("/api/bot-service/users/42/state", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["clients"]) == 1
    assert body["clients"][0]["inbound_tag"] == "DE-vless"
    assert body["expires_at_ms"] == expiry


def test_get_user_state_requires_token(app_with_service_api, db, client):
    resp = client.get("/api/bot-service/users/42/state")
    assert resp.status_code == 401


def test_bot_service_lists_tariffs_filtered(app_with_service_api, db, client, service_headers):
    from app.models import Tariff, TariffItem, UserTariffAccess

    with app_with_service_api.app_context():
        public = Tariff(
            name="Pub",
            price_rub=100,
            period_days=30,
            visibility="public",
            enabled=True,
            is_trial=False,
            sort_order=1,
        )
        public.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        private_for_42 = Tariff(
            name="Vip42",
            price_rub=999,
            period_days=30,
            visibility="private",
            enabled=True,
            is_trial=False,
            sort_order=2,
        )
        private_for_42.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        archived = Tariff(
            name="Old",
            price_rub=10,
            period_days=30,
            visibility="archived",
            enabled=True,
            is_trial=False,
        )
        db.session.add_all([public, private_for_42, archived])
        db.session.flush()
        db.session.add(
            UserTariffAccess(
                telegram_id=42,
                tariff_id=private_for_42.id,
                billing="paid",
            )
        )
        db.session.commit()

    resp = client.get(
        "/api/bot-service/tariffs?for=42",
        headers=service_headers,
    )
    body = resp.get_json()
    names = [t["name"] for t in body]
    assert "Pub" in names
    assert "Vip42" in names
    assert "Old" not in names

    resp_other = client.get(
        "/api/bot-service/tariffs?for=99",
        headers=service_headers,
    )
    other_names = [t["name"] for t in resp_other.get_json()]
    assert other_names == ["Pub"]


def test_bot_service_tariffs_no_for_param_all_inactive(app_with_service_api, db, client, service_headers):
    from app.models import Tariff, TariffItem

    with app_with_service_api.app_context():
        t = Tariff(
            name="Basic",
            price_rub=100,
            period_days=30,
            visibility="public",
            enabled=True,
            is_trial=False,
            sort_order=1,
        )
        t.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        db.session.add(t)
        db.session.commit()

    resp = client.get("/api/bot-service/tariffs", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body) == 1
    assert body[0]["is_active"] is False


def test_bot_service_tariffs_user_with_no_clients_all_inactive(app_with_service_api, db, client, service_headers):
    from app.models import Tariff, TariffItem, TelegramUser

    with app_with_service_api.app_context():
        t = Tariff(
            name="Basic",
            price_rub=100,
            period_days=30,
            visibility="public",
            enabled=True,
            is_trial=False,
            sort_order=1,
        )
        t.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        db.session.add_all([t, TelegramUser(telegram_id=42, language="ru")])
        db.session.commit()

    resp = client.get("/api/bot-service/tariffs?for=42", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body) == 1
    assert body[0]["is_active"] is False


def test_bot_service_tariffs_active_permanent_client_marks_active(app_with_service_api, db, client, service_headers):
    from app.models import Client, Inbound, Tariff, TariffItem, TelegramUser

    with app_with_service_api.app_context():
        inbound = Inbound(tag="vless-de", protocol="vless", port=10001, stream_settings="{}")
        t = Tariff(
            name="Basic",
            price_rub=100,
            period_days=30,
            visibility="public",
            enabled=True,
            is_trial=False,
            sort_order=1,
        )
        t.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        db.session.add_all([inbound, t, TelegramUser(telegram_id=42, language="ru")])
        db.session.flush()
        db.session.add(
            Client(
                id="c-1",
                email="tg42_vless-de",
                inbound_tag="vless-de",
                telegram_id=42,
                tariff_id=t.id,
                enable=True,
                expiry_time=0,
                limit_bytes=0,
            )
        )
        db.session.commit()

    resp = client.get("/api/bot-service/tariffs?for=42", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body[0]["is_active"] is True


def test_bot_service_tariffs_future_expiry_client_marks_active(app_with_service_api, db, client, service_headers):
    import time

    from app.models import Client, Inbound, Tariff, TariffItem, TelegramUser

    with app_with_service_api.app_context():
        inbound = Inbound(tag="vless-de", protocol="vless", port=10001, stream_settings="{}")
        t = Tariff(
            name="Basic",
            price_rub=100,
            period_days=30,
            visibility="public",
            enabled=True,
            is_trial=False,
            sort_order=1,
        )
        t.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        db.session.add_all([inbound, t, TelegramUser(telegram_id=42, language="ru")])
        db.session.flush()
        db.session.add(
            Client(
                id="c-1",
                email="tg42_vless-de",
                inbound_tag="vless-de",
                telegram_id=42,
                tariff_id=t.id,
                enable=True,
                expiry_time=int(time.time() * 1000) + 86_400_000,
                limit_bytes=0,
            )
        )
        db.session.commit()

    resp = client.get("/api/bot-service/tariffs?for=42", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body[0]["is_active"] is True


def test_bot_service_tariffs_expired_client_does_not_mark_active(app_with_service_api, db, client, service_headers):
    import time

    from app.models import Client, Inbound, Tariff, TariffItem, TelegramUser

    with app_with_service_api.app_context():
        inbound = Inbound(tag="vless-de", protocol="vless", port=10001, stream_settings="{}")
        t = Tariff(
            name="Basic",
            price_rub=100,
            period_days=30,
            visibility="public",
            enabled=True,
            is_trial=False,
            sort_order=1,
        )
        t.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        db.session.add_all([inbound, t, TelegramUser(telegram_id=42, language="ru")])
        db.session.flush()
        db.session.add(
            Client(
                id="c-1",
                email="tg42_vless-de",
                inbound_tag="vless-de",
                telegram_id=42,
                tariff_id=t.id,
                enable=True,
                expiry_time=int(time.time() * 1000) - 86_400_000,
                limit_bytes=0,
            )
        )
        db.session.commit()

    resp = client.get("/api/bot-service/tariffs?for=42", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body[0]["is_active"] is False


def test_bot_service_tariffs_disabled_client_does_not_mark_active(app_with_service_api, db, client, service_headers):
    from app.models import Client, Inbound, Tariff, TariffItem, TelegramUser

    with app_with_service_api.app_context():
        inbound = Inbound(tag="vless-de", protocol="vless", port=10001, stream_settings="{}")
        t = Tariff(
            name="Basic",
            price_rub=100,
            period_days=30,
            visibility="public",
            enabled=True,
            is_trial=False,
            sort_order=1,
        )
        t.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        db.session.add_all([inbound, t, TelegramUser(telegram_id=42, language="ru")])
        db.session.flush()
        db.session.add(
            Client(
                id="c-1",
                email="tg42_vless-de",
                inbound_tag="vless-de",
                telegram_id=42,
                tariff_id=t.id,
                enable=False,
                expiry_time=0,
                limit_bytes=0,
            )
        )
        db.session.commit()

    resp = client.get("/api/bot-service/tariffs?for=42", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body[0]["is_active"] is False


def test_bot_service_tariffs_only_owning_tariff_marked_active(app_with_service_api, db, client, service_headers):
    from app.models import Client, Inbound, Tariff, TariffItem, TelegramUser

    with app_with_service_api.app_context():
        inbound = Inbound(tag="vless-de", protocol="vless", port=10001, stream_settings="{}")
        t_owned = Tariff(
            name="Owned",
            price_rub=100,
            period_days=30,
            visibility="public",
            enabled=True,
            is_trial=False,
            sort_order=1,
        )
        t_owned.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        t_other = Tariff(
            name="Other",
            price_rub=200,
            period_days=30,
            visibility="public",
            enabled=True,
            is_trial=False,
            sort_order=2,
        )
        t_other.items = [TariffItem(inbound_tag="vless-de", traffic_gb=0)]
        db.session.add_all([inbound, t_owned, t_other, TelegramUser(telegram_id=42, language="ru")])
        db.session.flush()
        db.session.add(
            Client(
                id="c-1",
                email="tg42_vless-de",
                inbound_tag="vless-de",
                telegram_id=42,
                tariff_id=t_owned.id,
                enable=True,
                expiry_time=0,
                limit_bytes=0,
            )
        )
        db.session.commit()

    resp = client.get("/api/bot-service/tariffs?for=42", headers=service_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    by_name = {t["name"]: t for t in body}
    assert by_name["Owned"]["is_active"] is True
    assert by_name["Other"]["is_active"] is False
