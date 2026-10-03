import datetime as dt
from types import SimpleNamespace

import pytest

from panel_core.api import bot_service
from panel_core.extensions import db
from panel_core.models import Inbound, Payment, SystemSetting, Tariff, TariffItem, UserTariffAccess
from panel_core.services import billing


@pytest.fixture
def private_tariff(app, monkeypatch):
    app.register_blueprint(bot_service.bp, url_prefix="/api")
    db.session.add_all(
        [
            Inbound(tag="private", protocol="vless", port=14443, stream_settings="{}"),
            SystemSetting(key="bot_service_token", value="test-token"),
            SystemSetting(key="yookassa_shop_id", value="test-shop"),
            SystemSetting(key="yookassa_secret_key", value="test-secret"),
        ]
    )
    tariff = Tariff(name="Private", price_rub=100, period_days=30, visibility="private", enabled=True)
    tariff.items = [TariffItem(inbound_tag="private", traffic_gb=1)]
    db.session.add(tariff)
    db.session.commit()

    def provider_create(payload, key):
        return SimpleNamespace(
            id="yk-private",
            amount=SimpleNamespace(**payload["amount"]),
            metadata=payload["metadata"],
            status="pending",
            confirmation=SimpleNamespace(confirmation_url="https://yookassa.test/private"),
        )

    monkeypatch.setattr(billing.yookassa.Payment, "create", provider_create)
    return tariff


@pytest.mark.parametrize("surface", ["catalog", "checkout"])
@pytest.mark.parametrize(
    "kind,status,term_days,allowed",
    [
        ("paid", "revoked", -1, False),
        ("paid", "revoking", None, False),
        ("paid", "revoking", 1, False),
        ("paid", "pending", None, False),
        ("free", "revoked", 1, False),
        ("free", "revoking", None, False),
        ("free", "pending", 1, False),
        ("free", "succeeded", -1, False),
        ("free", "succeeded", 1, True),
        ("free", "succeeded", None, False),
        ("paid", "succeeded", None, True),
        ("paid", "succeeded", -1, True),
        ("paid", None, None, True),
    ],
)
def test_private_tariff_requires_current_access(app, private_tariff, surface, kind, status, term_days, allowed):
    access = UserTariffAccess(
        telegram_id=42,
        tariff_id=private_tariff.id,
        billing=kind,
        access_until=None
        if term_days is None
        else dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(days=term_days),
    )
    if status is not None:
        access.provisioning_status = status
    db.session.add(access)
    db.session.commit()

    if surface == "catalog":
        response = app.test_client().get(
            "/api/bot-service/tariffs?for=42", headers={"Authorization": "Bearer test-token"}
        )
        assert response.status_code == 200
        assert [tariff["id"] for tariff in response.get_json()] == ([private_tariff.id] if allowed else [])
    elif allowed:
        result = billing.create_checkout(telegram_id=42, tariff_id=private_tariff.id, lang="ru")
        assert result["confirmation_url"] == "https://yookassa.test/private"
        assert Payment.query.count() == 1
    else:
        reason = (
            "open_ended_access"
            if kind == "free" and status == "succeeded" and term_days is None
            else "tariff_not_available"
        )
        with pytest.raises(ValueError, match=reason):
            billing.create_checkout(telegram_id=42, tariff_id=private_tariff.id, lang="ru")
        assert Payment.query.count() == 0


@pytest.mark.parametrize("access_status", ["revoking", "revoked"])
@pytest.mark.parametrize("checkout_status", ["creating", "ready"])
def test_revoked_invitation_cannot_resume_existing_checkout(private_tariff, access_status, checkout_status):
    access = UserTariffAccess(telegram_id=42, tariff_id=private_tariff.id, billing="paid")
    db.session.add(access)
    db.session.commit()
    result = billing.create_checkout(telegram_id=42, tariff_id=private_tariff.id, lang="ru")
    payment = db.session.get(Payment, result["payment_id"])
    access.provisioning_status = access_status
    payment.checkout_status = checkout_status
    if checkout_status == "creating":
        payment.yookassa_id = "pending-interrupted-checkout"
    db.session.commit()

    with pytest.raises(ValueError, match="tariff_not_available"):
        billing.create_checkout(telegram_id=42, tariff_id=private_tariff.id, lang="ru")

    assert Payment.query.count() == 1
    assert payment.checkout_status == checkout_status
    assert payment.provider_status == "pending"
    assert payment.cancel_requested_at is None
