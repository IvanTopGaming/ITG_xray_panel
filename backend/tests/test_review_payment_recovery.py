import datetime as dt
from types import SimpleNamespace

import pytest
from flask import Flask

from panel_core.extensions import db
from panel_core.models import BotEvent, Payment, SystemSetting, Tariff, TariffItem, TelegramUser
from panel_core.services import billing
from panel_core.jobs import payments


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'payments.db'}", SQLALCHEMY_TRACK_MODIFICATIONS=False
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        db.session.add_all(
            [
                SystemSetting(key="yookassa_shop_id", value="shop"),
                SystemSetting(key="yookassa_secret_key", value="secret"),
            ]
        )
        db.session.commit()
        monkeypatch.setattr(billing.tariff_delivery, "is_deliverable", lambda tariff: True)
        yield app
        db.session.remove()
        db.drop_all()


def purchase(status="pending", age=60):
    tariff = Tariff(name="Original", price_rub=100, period_days=30, enabled=True, visibility="public", is_trial=False)
    db.session.add(tariff)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="vpn", panel_id=1, traffic_gb=1))
    db.session.flush()
    payment = Payment(
        yookassa_id=f"yk-{tariff.id}",
        telegram_id=42,
        tariff_id=tariff.id,
        tariff_snapshot={
            "name": "Original",
            "period_days": 30,
            "price_rub": 100,
            "visibility": "public",
            "is_trial": False,
            "items": [{"inbound_tag": "vpn", "panel_id": 1, "traffic_gb": 1}],
        },
        amount_rub=100,
        status=status,
        confirmation_url="https://checkout",
        created_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(seconds=age),
    )
    db.session.add(payment)
    db.session.commit()
    return payment, tariff


def remote(payment, refunded="0.00", status="succeeded"):
    return SimpleNamespace(
        id=payment.yookassa_id,
        status=status,
        amount=SimpleNamespace(value="100.00", currency="RUB"),
        refunded_amount=SimpleNamespace(value=refunded, currency="RUB"),
        metadata={"payment_db_id": payment.id, "telegram_id": 42, "tariff_id": payment.tariff_id},
    )


def test_paid_purchase_keeps_its_original_terms_after_tariff_edit(ledger, monkeypatch):
    payment, tariff = purchase()
    tariff.period_days = 1
    tariff.items[0].traffic_gb = 999
    db.session.commit()
    received = []
    monkeypatch.setattr(
        billing.provisioning,
        "apply_tariff_for_user",
        lambda **kw: received.append(kw["tariff"]) or {"expires_at_ms": 123},
    )
    billing.apply_payment(payment)
    assert received[0].period_days == 30
    assert received[0].items[0].traffic_gb == 1


def test_partial_refund_keeps_access(ledger, monkeypatch):
    payment, _ = purchase("succeeded")
    monkeypatch.setattr(billing.yookassa.Payment, "find_one", lambda _: remote(payment, "1.00"))
    revoked = []
    monkeypatch.setattr(billing.provisioning, "revoke_payment_access", lambda *a, **kw: revoked.append(a) or {})
    billing.handle_refund(payment)
    assert revoked == []
    assert payment.status == "succeeded"


def test_full_refund_retries_failed_node_delivery(ledger, monkeypatch):
    payment, _ = purchase("succeeded")
    monkeypatch.setattr(billing.yookassa.Payment, "find_one", lambda _: remote(payment, "100.00"))
    revoked = []
    monkeypatch.setattr(
        billing.provisioning,
        "revoke_payment_access",
        lambda *a, **kw: revoked.append(kw) or {"panel_failures": [{"panel_id": 1}]},
    )
    billing.handle_refund(payment)
    billing.handle_refund(payment)
    assert len(revoked) == 2


def test_blocked_paid_user_is_not_provisioned(ledger, monkeypatch):
    payment, _ = purchase()
    db.session.add(TelegramUser(telegram_id=42, blocked=True))
    db.session.commit()
    issued = []
    monkeypatch.setattr(
        billing.provisioning, "apply_tariff_for_user", lambda **kw: issued.append(kw) or {"expires_at_ms": 123}
    )
    billing.apply_payment(payment)
    assert issued == []


def test_cleanup_does_not_cancel_authoritative_pending(ledger, monkeypatch):
    payment, _ = purchase(age=90000)
    monkeypatch.setattr(billing, "fetch_remote_status", lambda _: "pending")
    monkeypatch.setattr(billing.yookassa.Payment, "find_one", lambda _: remote(payment, status="pending"))
    payments.cleanup_old_payments()
    assert payment.status == "pending"


@pytest.mark.parametrize("kind", ["pending", "succeeded"])
def test_payment_queues_progress_past_first_batch(ledger, monkeypatch, kind):
    rows = [purchase(kind)[0] for _ in range(201)]
    checked = []
    by_id = {p.yookassa_id: p for p in rows}
    monkeypatch.setattr(
        billing.yookassa.Payment, "find_one", lambda key: checked.append(key) or remote(by_id[key], status=kind)
    )
    job = payments.poll_pending_payments if kind == "pending" else payments.reconcile_refunds
    job()
    job()
    assert len(set(checked)) == 201


def test_checkout_response_loss_reuses_the_saved_invoice(ledger, monkeypatch):
    _, tariff = purchase()
    requests = []

    def create(payload, key):
        requests.append((payload, key))
        return SimpleNamespace(
            id="recovered-invoice",
            status="pending",
            amount=SimpleNamespace(value="100.00", currency="RUB"),
            metadata=payload["metadata"],
            confirmation=SimpleNamespace(confirmation_url="https://invoice"),
        )

    monkeypatch.setattr(billing.yookassa.Payment, "create", create)
    first = billing.create_checkout(telegram_id=42, tariff_id=tariff.id, lang="ru")
    second = billing.create_checkout(telegram_id=42, tariff_id=tariff.id, lang="ru")
    assert second["payment_id"] == first["payment_id"]
    assert len(requests) == 1


def test_interrupted_provider_creation_replays_exact_saved_request(ledger, monkeypatch):
    _, tariff = purchase()
    requests = []

    def create(payload, key):
        requests.append((payload, key))
        if len(requests) == 1:
            raise RuntimeError("lost reply")
        return SimpleNamespace(
            id="recovered-invoice",
            status="pending",
            amount=SimpleNamespace(value="100.00", currency="RUB"),
            metadata=payload["metadata"],
            confirmation=SimpleNamespace(confirmation_url="https://invoice"),
        )

    monkeypatch.setattr(billing.yookassa.Payment, "create", create)
    with pytest.raises(RuntimeError, match="lost reply"):
        billing.create_checkout(telegram_id=42, tariff_id=tariff.id, lang="ru")
    tariff_id = tariff.id
    db.session.remove()
    tariff = db.session.get(Tariff, tariff_id)
    tariff.price_rub = 999
    db.session.commit()
    result = billing.create_checkout(telegram_id=42, tariff_id=tariff.id, lang="ru")
    assert requests[0] == requests[1]
    assert result["amount_rub"] == 100


def test_expired_creation_key_never_creates_another_remote_invoice(ledger, monkeypatch):
    payment, _ = purchase()
    payment.checkout_status = "creating"
    payment.provider_idempotency_key = "old-key"
    payment.checkout_payload = {"old": "request"}
    payment.checkout_started_at = billing._now() - dt.timedelta(days=2)
    db.session.commit()
    monkeypatch.setattr(billing.yookassa.Payment, "create", lambda *a: pytest.fail("expired key reused"))
    with pytest.raises(ValueError, match="checkout_requires_review"):
        billing.resume_checkout(payment)
    assert payment.checkout_status == "review"


def test_provider_metadata_recovers_placeholder_but_rejects_wrong_amount(ledger, monkeypatch):
    payment, _ = purchase()
    payment.yookassa_id = "pending-placeholder"
    db.session.commit()
    obj = remote(payment)
    obj.id = "real-id"
    obj.amount.value = "99.00"
    monkeypatch.setattr(billing.yookassa.Payment, "find_one", lambda _: obj)
    with pytest.raises(ValueError, match="amount_mismatch"):
        billing.resolve_remote_payment("real-id")
    assert payment.yookassa_id == "pending-placeholder"
    obj.amount.value = "100.00"
    recovered, _ = billing.resolve_remote_payment("real-id")
    assert recovered.id == payment.id
    assert recovered.yookassa_id == "real-id"


def test_old_owner_failure_cannot_downgrade_new_owner_success(ledger, monkeypatch):
    payment, _ = purchase(age=90000)
    payment_id = payment.id
    nesting = []

    def deliver(**kw):
        if nesting:
            return {"expires_at_ms": 123}
        nesting.append(True)
        with ledger.app_context():
            concurrent = db.session.get(Payment, payment_id)
            concurrent.processing_expires_at = billing._now() - dt.timedelta(seconds=1)
            db.session.commit()
            payments.release_stranded_claims()
            billing.apply_payment(concurrent)
        raise RuntimeError("old owner failed late")

    monkeypatch.setattr(billing.provisioning, "apply_tariff_for_user", deliver)
    with pytest.raises(RuntimeError, match="old owner failed late"):
        billing.apply_payment(payment)
    db.session.refresh(payment)
    assert payment.status == "succeeded"
    assert payment.fulfillment_status == "succeeded"
    assert BotEvent.query.filter_by(type="payment_succeeded").count() == 1


def test_live_processing_heartbeat_survives_purchase_age_cleanup(ledger, monkeypatch):
    import gevent

    payment, _ = purchase(age=90000)
    monkeypatch.setattr(billing, "_LEASE_SECONDS", 0.15)
    monkeypatch.setattr(billing, "_HEARTBEAT_SECONDS", 0.02)

    def deliver(**kw):
        gevent.sleep(0.2)
        with ledger.app_context():
            payments.release_stranded_claims()
        kw["guard"]()
        return {"expires_at_ms": 123}

    monkeypatch.setattr(billing.provisioning, "apply_tariff_for_user", deliver)
    billing.apply_payment(payment)
    assert payment.status == "succeeded"
    assert payment.processing_owner is None


def test_unknown_purchased_terms_are_a_visible_paid_obligation(ledger, monkeypatch):
    payment, _ = purchase()
    payment.tariff_snapshot = {"name": "unknown"}
    db.session.commit()
    monkeypatch.setattr(billing.provisioning, "apply_tariff_for_user", lambda **kw: pytest.fail("guessed terms"))
    billing.apply_payment(payment)
    assert payment.provider_status == "succeeded"
    assert payment.fulfillment_status == "review"
    assert BotEvent.query.filter_by(type="payment_succeeded").count() == 0


def test_full_refund_seen_with_success_never_issues_access(ledger, monkeypatch):
    payment, _ = purchase()
    monkeypatch.setattr(billing.yookassa.Payment, "find_one", lambda _: remote(payment, "100.00"))
    issued = []
    monkeypatch.setattr(
        billing.provisioning, "apply_tariff_for_user", lambda **kw: issued.append(kw) or {"expires_at_ms": 123}
    )
    payments.poll_pending_payments()
    assert issued == []
    assert payment.refunded_amount_kopeks == 10000


def test_local_close_and_legacy_cancel_still_accept_paid_money(ledger, monkeypatch):
    from panel_core.api.bot_service import cancel_payment_for_bot

    payment, _ = purchase()
    with ledger.test_request_context(json={"telegram_id": 42}):
        response = cancel_payment_for_bot.__wrapped__(payment.id)
    assert response.json["ui_closed"] is True
    assert response.json["status"] == "pending"
    payment.status = "cancelled"
    db.session.commit()
    monkeypatch.setattr(billing.provisioning, "apply_tariff_for_user", lambda **kw: {"expires_at_ms": 123})
    billing.accept_remote_status(payment, "succeeded")
    assert payment.fulfillment_status == "succeeded"
    assert payment.provider_status == "succeeded"


def test_refund_failure_survives_session_restart_with_purchase_identity(ledger, monkeypatch):
    payment, _ = purchase("succeeded")
    payment_id = payment.id
    monkeypatch.setattr(billing.yookassa.Payment, "find_one", lambda _: remote(payment, "100.00"))
    calls = []

    def revoke(*args, **kw):
        calls.append(kw)
        return (
            {"panel_failures": [{"panel_id": 7, "inbound_tag": "vpn"}]} if len(calls) == 1 else {"panel_failures": []}
        )

    monkeypatch.setattr(billing.provisioning, "revoke_payment_access", revoke)
    billing.handle_refund(payment)
    db.session.remove()
    payment = db.session.get(Payment, payment_id)
    billing.handle_refund(payment)
    assert calls[1]["operation_id"] == f"pay:{payment_id}"
    assert calls[1]["pending_targets"] == [{"panel_id": 7, "inbound_tag": "vpn"}]
    assert payment.refund_status == "completed"
    assert BotEvent.query.filter_by(type="payment_refunded").count() == 1


def test_admin_payment_list_uses_exclusive_utc_end_and_pagination(ledger):
    from panel_core.api.bot_admin import list_payments

    first, _ = purchase()
    second, _ = purchase()
    first.created_at = dt.datetime(2026, 9, 12, 21)
    second.created_at = dt.datetime(2026, 9, 13, 21)
    db.session.commit()
    with ledger.test_request_context("/?from=2026-09-12T21:00:00Z&to_exclusive=2026-09-13T21:00:00Z&limit=1&offset=0"):
        response = list_payments.__wrapped__()
    assert response.json["total"] == 1
    assert response.json["items"][0]["id"] == first.id
    assert response.json["items"][0]["created_at"].endswith("Z")


def test_admin_payment_stats_count_money_even_when_access_is_blocked(ledger):
    from panel_core.api.bot_admin import list_payments

    payment, _ = purchase()
    payment.provider_status = "succeeded"
    payment.fulfillment_status = "blocked"
    payment.fulfillment_error = "account_blocked"
    payment.refunded_amount_kopeks = 150
    payment.refund_status = "partial"
    payment.paid_at = billing._now()
    db.session.commit()
    with ledger.test_request_context("/?limit=1&offset=0"):
        response = list_payments.__wrapped__()
    body = response.json
    assert body["stats"]["month_count"] == 1
    assert body["stats"]["month_amount_rub"] == 98.5
    assert body["items"][0]["fulfillment_status"] == "blocked"
    assert body["items"][0]["fulfillment_error"] == "account_blocked"


def test_partial_refund_does_not_complete_blocked_payment_delivery(ledger, monkeypatch):
    payment, _ = purchase()
    user = TelegramUser(telegram_id=42, blocked=True)
    db.session.add(user)
    db.session.commit()
    monkeypatch.setattr(billing.yookassa.Payment, "find_one", lambda _: remote(payment, "1.00"))
    issued = []
    monkeypatch.setattr(
        billing.provisioning, "apply_tariff_for_user", lambda **kw: issued.append(kw) or {"expires_at_ms": 123}
    )
    billing.apply_payment(payment)
    billing.handle_refund(payment)
    user.blocked = False
    db.session.commit()
    billing.apply_payment(payment)
    assert payment.fulfillment_status == "succeeded"
    assert len(issued) == 1
