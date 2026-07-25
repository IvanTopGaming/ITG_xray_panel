import pytest

from panel_core.models import NotificationClaim, SystemSetting, Tariff, TelegramUser, UserTariffAccess
from panel_core.services.notifications import claim_notification


@pytest.fixture
def app_with_service_api(app, db):
    from panel_core.api import bot_service

    if not any(bp.name == "bot_service" for bp in app.blueprints.values()):
        app.register_blueprint(bot_service.bp, url_prefix="/api")
    db.session.add(SystemSetting(key="bot_service_token", value="secret-abc"))
    db.session.commit()
    return app


@pytest.fixture
def client(app_with_service_api):
    return app_with_service_api.test_client()


@pytest.fixture
def service_headers():
    return {"Authorization": "Bearer secret-abc"}


def test_first_claim_wins_second_is_refused(app, db):
    first = claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=7, scope="")
    second = claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=7, scope="")
    assert first["claimed"] is True
    assert second["claimed"] is False
    assert NotificationClaim.query.count() == 1


def test_traffic_claims_from_different_nodes_do_not_collide(app, db):
    de = claim_notification(
        telegram_id=42, kind="traffic_80", tariff_id=7, scope="de1.example.com/vless-reality/tg42_vless-reality"
    )
    nl = claim_notification(
        telegram_id=42, kind="traffic_80", tariff_id=7, scope="nl1.example.com/vless-reality/tg42_vless-reality"
    )
    assert de["claimed"] is True
    assert nl["claimed"] is True
    assert NotificationClaim.query.count() == 2


def test_claim_without_tariff_still_dedups(app, db):
    first = claim_notification(telegram_id=42, kind="expired", tariff_id=None, scope="")
    second = claim_notification(telegram_id=42, kind="expired", tariff_id=None, scope="")
    assert first["claimed"] is True
    assert second["claimed"] is False
    assert NotificationClaim.query.count() == 1
    assert NotificationClaim.query.first().tariff_id == 0


def test_claim_survives_refusal_and_keeps_session_usable(app, db):
    claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=7, scope="")
    claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=7, scope="")
    later = claim_notification(telegram_id=42, kind="expiry_1h", tariff_id=7, scope="")
    assert later["claimed"] is True
    assert NotificationClaim.query.count() == 2


def test_different_kinds_and_users_do_not_collide(app, db):
    same_kind_other_user = claim_notification(telegram_id=43, kind="expiry_1d", tariff_id=7, scope="")
    other_tariff = claim_notification(telegram_id=43, kind="expiry_1d", tariff_id=8, scope="")
    assert same_kind_other_user["claimed"] is True
    assert other_tariff["claimed"] is True


def test_claim_resolves_language_from_telegram_user(app, db):
    db.session.add(TelegramUser(telegram_id=42, language="en"))
    db.session.commit()
    result = claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=None, scope="")
    assert result["lang"] == "en"


def test_claim_defaults_language_to_ru_for_unknown_user(app, db):
    result = claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=None, scope="")
    assert result["lang"] == "ru"


def test_claim_marks_public_enabled_tariff_renewable(app, db):
    db.session.add(Tariff(id=7, name="Base", price_rub=100, period_days=30, visibility="public", enabled=True))
    db.session.commit()
    result = claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=7, scope="")
    assert result["renewable"] is True


def test_claim_marks_archived_tariff_not_renewable(app, db):
    db.session.add(Tariff(id=8, name="Old", price_rub=100, period_days=30, visibility="archived", enabled=True))
    db.session.commit()
    result = claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=8, scope="")
    assert result["renewable"] is False


def test_claim_without_tariff_is_not_renewable(app, db):
    result = claim_notification(telegram_id=42, kind="expired", tariff_id=None, scope="")
    assert result["renewable"] is False


def test_claim_private_tariff_needs_a_grant(app, db):
    db.session.add(Tariff(id=9, name="VIP", price_rub=100, period_days=30, visibility="private", enabled=True))
    db.session.commit()
    without = claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=9, scope="")

    db.session.add(UserTariffAccess(telegram_id=43, tariff_id=9, billing="paid"))
    db.session.commit()
    with_grant = claim_notification(telegram_id=43, kind="expiry_1d", tariff_id=9, scope="")
    assert without["renewable"] is False
    assert with_grant["renewable"] is True


def test_refused_claim_still_reports_lang_and_renewable(app, db):
    db.session.add(TelegramUser(telegram_id=42, language="en"))
    db.session.add(Tariff(id=7, name="Base", price_rub=100, period_days=30, visibility="public", enabled=True))
    db.session.commit()
    claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=7, scope="")
    second = claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=7, scope="")
    assert second["claimed"] is False
    assert second["lang"] == "en"
    assert second["renewable"] is True


def test_claim_endpoint_requires_bot_token(client):
    resp = client.post("/api/bot-service/notifications/claim", json={"telegram_id": 42, "kind": "expiry_1d"})
    assert resp.status_code == 401


def test_claim_endpoint_returns_verdict(client, service_headers):
    body = {"telegram_id": 42, "kind": "expiry_1d", "tariff_id": None, "scope": ""}
    first = client.post("/api/bot-service/notifications/claim", json=body, headers=service_headers)
    second = client.post("/api/bot-service/notifications/claim", json=body, headers=service_headers)
    assert first.status_code == 200
    assert first.get_json()["claimed"] is True
    assert second.status_code == 200
    assert second.get_json()["claimed"] is False


def test_claim_endpoint_reports_lang_and_renewable(app_with_service_api, db, client, service_headers):
    db.session.add(TelegramUser(telegram_id=42, language="en"))
    db.session.add(Tariff(id=7, name="Base", price_rub=100, period_days=30, visibility="public", enabled=True))
    db.session.commit()
    resp = client.post(
        "/api/bot-service/notifications/claim",
        json={"telegram_id": 42, "kind": "expiry_1d", "tariff_id": 7, "scope": ""},
        headers=service_headers,
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"claimed": True, "lang": "en", "renewable": True}


def test_claim_endpoint_rejects_missing_fields(client, service_headers):
    resp = client.post("/api/bot-service/notifications/claim", json={"telegram_id": 42}, headers=service_headers)
    assert resp.status_code == 400
    resp = client.post("/api/bot-service/notifications/claim", json={"kind": "expiry_1d"}, headers=service_headers)
    assert resp.status_code == 400


def test_claim_endpoint_rejects_non_numeric_telegram_id(client, service_headers):
    resp = client.post(
        "/api/bot-service/notifications/claim",
        json={"telegram_id": "not-a-number", "kind": "expiry_1d"},
        headers=service_headers,
    )
    assert resp.status_code == 400


def test_claim_endpoint_scope_separates_nodes(app_with_service_api, db, client, service_headers):
    de = client.post(
        "/api/bot-service/notifications/claim",
        json={"telegram_id": 42, "kind": "traffic_80", "tariff_id": 7, "scope": "de1/vless/tg42"},
        headers=service_headers,
    )
    nl = client.post(
        "/api/bot-service/notifications/claim",
        json={"telegram_id": 42, "kind": "traffic_80", "tariff_id": 7, "scope": "nl1/vless/tg42"},
        headers=service_headers,
    )
    assert de.get_json()["claimed"] is True
    assert nl.get_json()["claimed"] is True


def test_provisioning_clears_claims_for_the_tariff(app, db):
    claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=7, scope="")
    claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=8, scope="")

    from panel_core.services.provisioning import clear_notification_claims

    clear_notification_claims(telegram_id=42, tariff_id=7)

    remaining = {(c.tariff_id, c.kind) for c in NotificationClaim.query.all()}
    assert remaining == {(8, "expiry_1d")}


def test_claim_can_be_reacquired_after_reset(app, db):
    from panel_core.services.provisioning import clear_notification_claims

    first = claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=7, scope="")
    clear_notification_claims(telegram_id=42, tariff_id=7)
    again = claim_notification(telegram_id=42, kind="expiry_1d", tariff_id=7, scope="")
    assert first["claimed"] is True
    assert again["claimed"] is True


def test_tariffless_expiry_claims_are_per_client(app, db):
    first = claim_notification(
        telegram_id=42, kind="expired", tariff_id=None, scope="de1.example.com/vless-reality/admin_de"
    )
    second = claim_notification(
        telegram_id=42, kind="expired", tariff_id=None, scope="de1.example.com/vless-reality/admin_nl"
    )
    repeat = claim_notification(
        telegram_id=42, kind="expired", tariff_id=None, scope="de1.example.com/vless-reality/admin_de"
    )
    assert first["claimed"] is True
    assert second["claimed"] is True
    assert repeat["claimed"] is False


def test_real_tariff_expiry_still_collapses_across_nodes(app, db):
    de = claim_notification(telegram_id=42, kind="expiry_1h", tariff_id=7, scope="")
    nl = claim_notification(telegram_id=42, kind="expiry_1h", tariff_id=7, scope="")
    assert de["claimed"] is True
    assert nl["claimed"] is False


def test_traffic_claim_is_reacquired_after_a_monthly_reset(app, db):
    base = "de1.example.com/vless-reality/tg42_vless-reality"
    first = claim_notification(telegram_id=42, kind="traffic_80", tariff_id=7, scope=f"{base}/1753000000000")
    same_cycle = claim_notification(telegram_id=42, kind="traffic_80", tariff_id=7, scope=f"{base}/1753000000000")
    next_cycle = claim_notification(telegram_id=42, kind="traffic_80", tariff_id=7, scope=f"{base}/1755678400000")
    assert first["claimed"] is True
    assert same_cycle["claimed"] is False
    assert next_cycle["claimed"] is True
