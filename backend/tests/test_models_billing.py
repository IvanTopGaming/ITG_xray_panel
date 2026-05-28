"""Unit tests for billing-related SQLAlchemy models."""

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError


def test_tariff_create_minimal(app, db):
    from app.models import Tariff

    t = Tariff(name="Standard", price_rub=150, period_days=30)
    db.session.add(t)
    db.session.commit()
    assert t.id is not None
    assert t.visibility == "public"
    assert t.is_trial is False
    assert t.enabled is True
    assert t.sort_order == 0


def test_tariff_visibility_values(app, db):
    from app.models import Tariff

    for v in ("public", "private", "archived"):
        t = Tariff(name=f"t-{v}", price_rub=100, period_days=30, visibility=v)
        db.session.add(t)
    db.session.commit()


def test_tariff_item_attached_to_tariff(app, db):
    from app.models import Tariff, TariffItem

    t = Tariff(name="Standard", price_rub=150, period_days=30)
    db.session.add(t)
    db.session.flush()
    item = TariffItem(
        tariff_id=t.id,
        inbound_tag="DE-vless",
        label="Germany",
        traffic_gb=0,
    )
    db.session.add(item)
    db.session.commit()
    assert len(t.items) == 1
    assert t.items[0].label == "Germany"
    assert t.items[0].traffic_gb == 0
    assert t.items[0].panel_id is None


def test_tariff_item_cascade_delete(app, db):
    from app.models import Tariff, TariffItem

    t = Tariff(name="Standard", price_rub=150, period_days=30)
    db.session.add(t)
    db.session.flush()
    item = TariffItem(tariff_id=t.id, inbound_tag="DE", traffic_gb=10)
    db.session.add(item)
    db.session.commit()
    item_id = item.id
    db.session.delete(t)
    db.session.commit()
    assert db.session.get(TariffItem, item_id) is None


def test_user_tariff_access_free(app, db):
    from app.models import Tariff, UserTariffAccess

    t = Tariff(name="Free", price_rub=0, period_days=30)
    db.session.add(t)
    db.session.flush()
    a = UserTariffAccess(
        telegram_id=12345,
        tariff_id=t.id,
        billing="free",
        next_renewal_at=datetime(2026, 6, 1),
    )
    db.session.add(a)
    db.session.commit()
    assert a.id is not None
    assert a.billing == "free"


def test_user_tariff_access_unique_per_user(app, db):
    from app.models import Tariff, UserTariffAccess

    t = Tariff(name="X", price_rub=100, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add(UserTariffAccess(telegram_id=1, tariff_id=t.id, billing="free"))
    db.session.commit()
    db.session.add(UserTariffAccess(telegram_id=1, tariff_id=t.id, billing="paid"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_user_tariff_access_billing_values(app, db):
    from app.models import Tariff, UserTariffAccess

    t = Tariff(name="Y", price_rub=100, period_days=30)
    db.session.add(t)
    db.session.flush()
    a = UserTariffAccess(telegram_id=2, tariff_id=t.id, billing="paid")
    db.session.add(a)
    db.session.commit()
    assert a.next_renewal_at is None


def test_payment_create(app, db):
    from app.models import Payment, Tariff

    t = Tariff(name="Standard", price_rub=150, period_days=30)
    db.session.add(t)
    db.session.flush()
    p = Payment(
        yookassa_id="2c5d8a-test-001",
        telegram_id=999,
        tariff_id=t.id,
        tariff_snapshot={"name": "Standard", "price_rub": 150, "period_days": 30},
        amount_rub=150,
        status="pending",
    )
    db.session.add(p)
    db.session.commit()
    assert p.id is not None
    assert p.metadata_json == {}
    assert p.paid_at is None


def test_payment_yookassa_id_unique(app, db):
    from app.models import Payment, Tariff

    t = Tariff(name="Standard", price_rub=150, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add(
        Payment(
            yookassa_id="dup",
            telegram_id=1,
            tariff_id=t.id,
            tariff_snapshot={},
            amount_rub=150,
            status="pending",
        )
    )
    db.session.commit()
    db.session.add(
        Payment(
            yookassa_id="dup",
            telegram_id=2,
            tariff_id=t.id,
            tariff_snapshot={},
            amount_rub=150,
            status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_bot_text_create(app, db):
    from app.models import BotText

    bt = BotText(key="welcome.title", lang="ru", text="Привет")
    db.session.add(bt)
    db.session.commit()
    assert db.session.get(BotText, ("welcome.title", "ru")).text == "Привет"


def test_bot_text_composite_pk(app, db):
    from app.models import BotText

    db.session.add(BotText(key="k", lang="ru", text="ru-text"))
    db.session.add(BotText(key="k", lang="en", text="en-text"))
    db.session.commit()
    assert db.session.get(BotText, ("k", "ru")).text == "ru-text"
    assert db.session.get(BotText, ("k", "en")).text == "en-text"
    db.session.add(BotText(key="k", lang="ru", text="dup"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_bot_event_create(app, db):
    from app.models import BotEvent

    e = BotEvent(
        type="payment_succeeded",
        telegram_id=42,
        payload={"payment_id": 7, "tariff_name": "Standard"},
    )
    db.session.add(e)
    db.session.commit()
    assert e.id is not None
    assert e.delivered_at is None
    assert e.payload["payment_id"] == 7


def test_bot_event_broadcast_no_telegram_id(app, db):
    from app.models import BotEvent

    e = BotEvent(type="texts_changed", telegram_id=None, payload={"lang": "ru"})
    db.session.add(e)
    db.session.commit()
    assert e.telegram_id is None


def test_telegram_user_create(app, db):
    from app.models import TelegramUser

    u = TelegramUser(telegram_id=12345, username="ivan", language="ru")
    db.session.add(u)
    db.session.commit()
    assert u.first_seen_at is not None
    assert u.last_seen_at is not None
    assert u.trial_used_at is None
    assert u.blocked is False


def test_telegram_user_pk_is_telegram_id(app, db):
    from app.models import TelegramUser

    db.session.add(TelegramUser(telegram_id=1))
    db.session.commit()
    assert db.session.get(TelegramUser, 1) is not None
    db.session.add(TelegramUser(telegram_id=1))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_telegram_user_language_default(app, db):
    from app.models import TelegramUser

    u = TelegramUser(telegram_id=99)
    db.session.add(u)
    db.session.commit()
    assert u.language == "ru"


def test_notification_log_create(app, db):
    from app.models import Client, Inbound, NotificationLog

    inbound = Inbound(tag="X", protocol="vless", port=443, stream_settings="{}")
    db.session.add(inbound)
    db.session.flush()
    client = Client(
        id="client-uuid-1",
        email="test@x",
        inbound_tag="X",
        limit_bytes=0,
        expiry_time=0,
        up=0,
        down=0,
        enable=True,
    )
    db.session.add(client)
    db.session.flush()

    n = NotificationLog(
        telegram_id=42,
        client_id=client.id,
        kind="expiry_3d",
    )
    db.session.add(n)
    db.session.commit()
    assert n.id is not None
    assert n.sent_at is not None


def test_notification_log_kinds(app, db):
    from app.models import Client, Inbound, NotificationLog

    inbound = Inbound(tag="X", protocol="vless", port=443, stream_settings="{}")
    db.session.add(inbound)
    db.session.flush()
    client = Client(
        id="client-uuid-2",
        email="t@x",
        inbound_tag="X",
        limit_bytes=0,
        expiry_time=0,
        up=0,
        down=0,
        enable=True,
    )
    db.session.add(client)
    db.session.flush()

    for kind in ("expiry_3d", "expiry_1d", "expiry_1h", "expired"):
        db.session.add(
            NotificationLog(
                telegram_id=1,
                client_id=client.id,
                kind=kind,
            )
        )
    db.session.commit()
    assert NotificationLog.query.count() == 4


def test_client_billing_columns_exist_and_nullable(app, db):
    from app.models import Client, Inbound

    inbound = Inbound(tag="CB1", protocol="vless", port=4451, stream_settings="{}")
    db.session.add(inbound)
    db.session.flush()

    legacy = Client(
        id="client-billing-legacy",
        email="legacy@x",
        inbound_tag="CB1",
        limit_bytes=0,
        expiry_time=0,
        up=0,
        down=0,
        enable=True,
    )
    db.session.add(legacy)
    db.session.commit()
    assert legacy.telegram_id is None
    assert legacy.tariff_id is None


def test_client_with_telegram_and_tariff(app, db):
    from app.models import Client, Inbound, Tariff

    inbound = Inbound(tag="CB2", protocol="vless", port=4452, stream_settings="{}")
    db.session.add(inbound)
    db.session.flush()
    t = Tariff(name="Standard", price_rub=150, period_days=30)
    db.session.add(t)
    db.session.flush()

    c = Client(
        id="client-billing-with-tariff",
        email="paid@x",
        inbound_tag="CB2",
        limit_bytes=0,
        expiry_time=0,
        up=0,
        down=0,
        enable=True,
        telegram_id=12345,
        tariff_id=t.id,
    )
    db.session.add(c)
    db.session.commit()
    assert c.telegram_id == 12345
    assert c.tariff_id == t.id


def test_client_query_by_telegram_id(app, db):
    from app.models import Client, Inbound

    inbound = Inbound(tag="CB3", protocol="vless", port=4453, stream_settings="{}")
    db.session.add(inbound)
    db.session.flush()
    db.session.add(
        Client(
            id="client-billing-tg-a",
            email="a@x",
            inbound_tag="CB3",
            limit_bytes=0,
            expiry_time=0,
            up=0,
            down=0,
            enable=True,
            telegram_id=999,
        )
    )
    db.session.add(
        Client(
            id="client-billing-tg-b",
            email="b@x",
            inbound_tag="CB3",
            limit_bytes=0,
            expiry_time=0,
            up=0,
            down=0,
            enable=True,
            telegram_id=999,
        )
    )
    db.session.commit()
    assert Client.query.filter_by(telegram_id=999).count() == 2
