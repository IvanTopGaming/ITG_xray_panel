from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import update

from app.extensions import db
from app.models import SystemSetting, Tariff, TariffItem


@pytest.fixture
def app(app):
    """Extend the base app fixture with the billing blueprint."""
    from app.api import billing as billing_api

    if not any(bp.name == "billing" for bp in app.blueprints.values()):
        app.register_blueprint(billing_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def bot_token(app):
    with app.app_context():
        db.session.add(SystemSetting(key="bot_service_token", value="bot-token-x"))
        db.session.add_all(
            [
                SystemSetting(key="yookassa_shop_id", value="test-shop"),
                SystemSetting(key="yookassa_secret_key", value="test_secret"),
            ]
        )
        db.session.commit()
    yield "bot-token-x"


@pytest.fixture
def public_tariff(app, bot_token):
    with app.app_context():
        t = Tariff(
            name="Standard 30d",
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


def test_checkout_requires_bot_token(client, public_tariff):
    resp = client.post(
        "/api/billing/checkout",
        json={"telegram_id": 42, "tariff_id": public_tariff, "lang": "ru"},
    )
    assert resp.status_code == 401


def test_checkout_success_returns_url(client, bot_token, public_tariff):
    with patch("app.services.billing.yookassa.Payment.create") as mock_create:
        mock_create.return_value = SimpleNamespace(
            id="yk-1", confirmation=SimpleNamespace(confirmation_url="https://yk.test/p/1")
        )
        resp = client.post(
            "/api/billing/checkout",
            headers={"Authorization": f"Bearer {bot_token}"},
            json={"telegram_id": 42, "tariff_id": public_tariff, "lang": "ru"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["confirmation_url"] == "https://yk.test/p/1"
    assert body["amount_rub"] == 150


def test_checkout_returns_400_for_unavailable_tariff(client, bot_token, public_tariff, app):
    db.session.execute(update(Tariff).where(Tariff.id == public_tariff).values(enabled=False))
    db.session.commit()
    db.session.expunge_all()
    resp = client.post(
        "/api/billing/checkout",
        headers={"Authorization": f"Bearer {bot_token}"},
        json={"telegram_id": 42, "tariff_id": public_tariff, "lang": "ru"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "tariff_not_available"


def test_checkout_returns_502_when_yookassa_raises(client, bot_token, public_tariff):
    with patch("app.services.billing.yookassa.Payment.create") as mock_create:
        mock_create.side_effect = RuntimeError("yookassa down")
        resp = client.post(
            "/api/billing/checkout",
            headers={"Authorization": f"Bearer {bot_token}"},
            json={"telegram_id": 42, "tariff_id": public_tariff, "lang": "ru"},
        )
    assert resp.status_code == 502
    assert resp.get_json()["error"] == "yookassa_unavailable"


def test_checkout_validates_required_fields(client, bot_token):
    resp = client.post(
        "/api/billing/checkout",
        headers={"Authorization": f"Bearer {bot_token}"},
        json={"telegram_id": 42},  # missing tariff_id, lang
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Webhook tests (Task 5)
# ---------------------------------------------------------------------------

from app.models import Payment  # noqa: E402


def _make_pending(app, public_tariff, yk_id="yk-pending-1"):
    with app.app_context():
        p = Payment(
            yookassa_id=yk_id,
            telegram_id=42,
            tariff_id=public_tariff,
            tariff_snapshot={"name": "x", "price_rub": 150, "period_days": 30, "items": []},
            amount_rub=150,
            status="pending",
            metadata_json={"lang": "ru"},
        )
        db.session.add(p)
        db.session.commit()
        return p.id


def test_webhook_processes_succeeded_payment(client, public_tariff, app):
    pid = _make_pending(app, public_tariff)
    with (
        patch("app.services.billing.yookassa.Payment.find_one") as mock_find,
        patch("app.services.billing.provisioning.apply_tariff_for_user") as mock_provision,
        patch("app.services.billing.bot_events.publish"),
    ):
        mock_find.return_value = SimpleNamespace(status="succeeded")
        mock_provision.return_value = {"clients": [], "expires_at_ms": 9999999999000, "source": "yookassa"}
        resp = client.post(
            "/api/billing/yookassa/webhook",
            json={"event": "payment.succeeded", "object": {"id": "yk-pending-1"}},
        )
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(Payment, pid).status == "succeeded"


def test_webhook_is_idempotent_on_repeat_delivery(client, public_tariff, app):
    _make_pending(app, public_tariff)
    with (
        patch("app.services.billing.yookassa.Payment.find_one") as mock_find,
        patch("app.services.billing.provisioning.apply_tariff_for_user") as mock_provision,
        patch("app.services.billing.bot_events.publish"),
    ):
        mock_find.return_value = SimpleNamespace(status="succeeded")
        mock_provision.return_value = {"clients": [], "expires_at_ms": 9999999999000, "source": "yookassa"}
        for _ in range(2):
            resp = client.post(
                "/api/billing/yookassa/webhook",
                json={"event": "payment.succeeded", "object": {"id": "yk-pending-1"}},
            )
            assert resp.status_code == 200
    # Provisioning called exactly once even though webhook hit twice.
    assert mock_provision.call_count == 1


def test_webhook_marks_cancelled(client, public_tariff, app):
    pid = _make_pending(app, public_tariff)
    with (
        patch("app.services.billing.yookassa.Payment.find_one") as mock_find,
        patch("app.services.billing.bot_events.publish") as mock_publish,
    ):
        mock_find.return_value = SimpleNamespace(status="canceled")
        resp = client.post(
            "/api/billing/yookassa/webhook",
            json={"event": "payment.canceled", "object": {"id": "yk-pending-1"}},
        )
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(Payment, pid).status == "cancelled"
    mock_publish.assert_called_once()
    assert mock_publish.call_args.args[0] == "payment_cancelled"


def test_webhook_returns_200_for_unknown_payment(client, public_tariff):
    resp = client.post(
        "/api/billing/yookassa/webhook",
        json={"event": "payment.succeeded", "object": {"id": "unknown-yk"}},
    )
    assert resp.status_code == 200


def test_webhook_does_nothing_when_status_lookup_unavailable(client, public_tariff, app):
    """If YooKassa can't be reached (timeout/misconfig), fetch_remote_status
    returns None. The webhook must not provision; the poll cron retries the
    still-pending row later."""
    pid = _make_pending(app, public_tariff)
    with (
        patch("app.services.billing.fetch_remote_status", return_value=None),
        patch("app.services.billing.provisioning.apply_tariff_for_user") as mock_provision,
        patch("app.services.billing.bot_events.publish"),
    ):
        resp = client.post(
            "/api/billing/yookassa/webhook",
            json={"event": "payment.succeeded", "object": {"id": "yk-pending-1"}},
        )
    assert resp.status_code == 200
    mock_provision.assert_not_called()
    with app.app_context():
        assert db.session.get(Payment, pid).status == "pending"


def test_webhook_ignores_spoofed_succeeded_when_yookassa_says_pending(client, public_tariff, app):
    """The webhook body is only a trigger, never proof of payment. An attacker
    who knows a real pending yookassa_id can POST a forged
    {"event": "payment.succeeded"} from any IP. The handler must re-verify the
    authoritative status via yookassa.Payment.find_one and refuse to provision
    when the real status is not 'succeeded'."""
    pid = _make_pending(app, public_tariff)
    with (
        patch("app.services.billing.yookassa.Payment.find_one") as mock_find,
        patch("app.services.billing.provisioning.apply_tariff_for_user") as mock_provision,
        patch("app.services.billing.bot_events.publish"),
    ):
        mock_find.return_value = SimpleNamespace(status="pending")  # YooKassa: not paid
        resp = client.post(
            "/api/billing/yookassa/webhook",
            json={"event": "payment.succeeded", "object": {"id": "yk-pending-1"}},
            environ_base={"REMOTE_ADDR": "1.2.3.4"},  # IP no longer gates anything
        )
    assert resp.status_code == 200
    mock_provision.assert_not_called()
    with app.app_context():
        assert db.session.get(Payment, pid).status == "pending"
