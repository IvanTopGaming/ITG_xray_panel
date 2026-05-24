"""Tests for POST /api/bot-service/trial/activate."""

from datetime import datetime
from unittest.mock import patch

import pytest

from app.models import (
    Client,
    Inbound,
    SystemSetting,
    Tariff,
    TariffItem,
    TelegramUser,
)


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


@pytest.fixture
def trial_setup(app_with_service_api, db):
    inbound = Inbound(tag="DE-vless", protocol="vless", port=10001, stream_settings="{}")
    db.session.add(inbound)
    db.session.flush()
    tariff = Tariff(name="Trial", price_rub=0, period_days=1, is_trial=True)
    db.session.add(tariff)
    db.session.flush()
    db.session.add(
        TariffItem(
            tariff_id=tariff.id,
            inbound_tag="DE-vless",
            traffic_gb=5,
            sort_order=0,
        )
    )
    db.session.commit()
    return tariff


def test_activate_trial_creates_client_and_marks_used(app_with_service_api, db, client, service_headers, trial_setup):
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()

    with patch("app.services.provisioning._sync_after_provision"):
        resp = client.post(
            "/api/bot-service/trial/activate",
            headers=service_headers,
            json={"telegram_id": 42},
        )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "clients" in body
    assert len(body["clients"]) == 1

    user = TelegramUser.query.get(42)
    assert user.trial_used_at is not None
    assert Client.query.filter_by(telegram_id=42).count() == 1


def test_activate_trial_rejected_if_already_used(app_with_service_api, db, client, service_headers, trial_setup):
    db.session.add(
        TelegramUser(
            telegram_id=42,
            language="ru",
            trial_used_at=datetime.utcnow(),
        )
    )
    db.session.commit()

    resp = client.post(
        "/api/bot-service/trial/activate",
        headers=service_headers,
        json={"telegram_id": 42},
    )
    assert resp.status_code == 409
    assert "already" in resp.get_data(as_text=True).lower()


def test_activate_trial_404_if_no_trial_tariff(app_with_service_api, db, client, service_headers):
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()
    resp = client.post(
        "/api/bot-service/trial/activate",
        headers=service_headers,
        json={"telegram_id": 42},
    )
    assert resp.status_code == 404


def test_activate_trial_creates_telegram_user_if_missing(
    app_with_service_api, db, client, service_headers, trial_setup
):
    """If no TelegramUser row exists, the endpoint creates one."""
    with patch("app.services.provisioning._sync_after_provision"):
        resp = client.post(
            "/api/bot-service/trial/activate",
            headers=service_headers,
            json={"telegram_id": 42},
        )
    assert resp.status_code == 200
    assert TelegramUser.query.get(42) is not None


def test_activate_trial_requires_token(app_with_service_api, db, client):
    resp = client.post(
        "/api/bot-service/trial/activate",
        json={"telegram_id": 42},
    )
    assert resp.status_code == 401


def test_activate_trial_publishes_event(app_with_service_api, db, client, service_headers, trial_setup):
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()

    with (
        patch("app.services.provisioning._sync_after_provision"),
        patch("app.services.bot_events.publish") as mock_publish,
    ):
        client.post(
            "/api/bot-service/trial/activate",
            headers=service_headers,
            json={"telegram_id": 42},
        )
    mock_publish.assert_called_once()
    assert mock_publish.call_args.args[0] == "trial_activated"
