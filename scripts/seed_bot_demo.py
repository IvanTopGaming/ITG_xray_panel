"""Populate the bot's billing tables with demo data.

Run inside the backend container, AFTER scripts/seed_demo.py (which creates
the panel-side inbounds and clients this script links against):

    docker compose exec backend python /app/scripts/seed_bot_demo.py

Idempotent: wipes anything tagged 'demo-' and re-creates.
"""

from __future__ import annotations

import random
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, "/app")

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (  # noqa: E402
    Client,
    Payment,
    Tariff,
    TariffItem,
    TelegramUser,
    UserTariffAccess,
)

random.seed(20260517)

DEMO_INBOUND_PREFIX = "demo-"
DEMO_TARIFF_NAME_PREFIX = "[demo] "
DEMO_NOTE_PREFIX = "demo:"


# ─── Tariff catalog ───────────────────────────────────────────────────────────
# (name, price_rub, period_days, is_trial, visibility, items)
# items: list of (inbound_tag, label, traffic_gb, allowed_node_groups)
TARIFFS = [
    (
        "Free Trial",
        0,
        1,
        True,
        "public",
        [(f"{DEMO_INBOUND_PREFIX}vless-reality-vision", "Trial · 5 GB", 5, "")],
    ),
    (
        "Basic 30d",
        199,
        30,
        False,
        "public",
        [(f"{DEMO_INBOUND_PREFIX}vless-reality-vision", "VLESS · 100 GB", 100, "")],
    ),
    (
        "Pro 90d",
        499,
        90,
        False,
        "public",
        [
            (f"{DEMO_INBOUND_PREFIX}vless-reality-vision", "VLESS · 500 GB", 500, ""),
            (f"{DEMO_INBOUND_PREFIX}vmess-ws", "VMess WS (CDN) · 500 GB", 500, ""),
        ],
    ),
    (
        "Premium 180d",
        899,
        180,
        False,
        "public",
        [
            (f"{DEMO_INBOUND_PREFIX}vless-reality-vision", "VLESS · unlimited", 0, ""),
            (f"{DEMO_INBOUND_PREFIX}trojan-tls", "Trojan · unlimited", 0, ""),
        ],
    ),
    (
        "Yearly Unlimited",
        1499,
        365,
        False,
        "public",
        [
            (f"{DEMO_INBOUND_PREFIX}vless-reality-vision", "VLESS · unlimited", 0, ""),
            (f"{DEMO_INBOUND_PREFIX}vmess-ws", "VMess WS · unlimited", 0, ""),
            (f"{DEMO_INBOUND_PREFIX}trojan-tls", "Trojan · unlimited", 0, ""),
        ],
    ),
    (
        "EU-only 60d",
        349,
        60,
        False,
        "private",
        [(f"{DEMO_INBOUND_PREFIX}vless-reality-vision", "VLESS EU only · 300 GB", 300, "eu")],
    ),
]


# ─── Telegram users ───────────────────────────────────────────────────────────
USERS = [
    # (telegram_id, username, language, trial_used_days_ago, blocked)
    (100001, "alice_vpn", "ru", None, False),
    (100002, "bob_proxy", "ru", 30, False),
    (100003, "carol", "en", 5, False),
    (100004, "dave", "ru", None, False),
    (100005, "eve_user", "ru", 90, False),
    (100006, "frank", "en", 2, False),
    (100007, "grace_test", "ru", None, False),
    (100008, "hank_devops", "ru", 60, False),
    (100009, "iris", "en", None, False),
    (100010, "jack", "ru", 15, False),
    (100011, "kate_eu", "en", 7, False),
    (100012, "leo_blocked", "ru", 200, True),
    (100013, "mallory_spam", "ru", 45, True),
    (100014, "niaj", "en", None, False),
    (100015, "olivia", "ru", 1, False),
    (100016, "peggy", "ru", None, False),
    (100017, "quinn", "en", 21, False),
    (100018, "ruth_pro", "ru", 120, False),
    (100019, "steve", "en", None, False),
    (100020, "trent_yearly", "ru", 365, False),
    (100021, "uma", "ru", None, False),
    (100022, "victor", "en", 10, False),
    (100023, "wendy_premium", "ru", 180, False),
    (100024, "xavier_test", "en", None, False),
    (100025, "yara_eu", "ru", 4, False),
]


def wipe():
    print("→ Wiping demo bot rows…")
    demo_tg_ids = [u[0] for u in USERS]
    Payment.query.filter(Payment.telegram_id.in_(demo_tg_ids)).delete(synchronize_session=False)
    UserTariffAccess.query.filter(UserTariffAccess.telegram_id.in_(demo_tg_ids)).delete(
        synchronize_session=False
    )
    Client.query.filter(Client.telegram_id.in_(demo_tg_ids)).update(
        {"telegram_id": None}, synchronize_session=False
    )
    TelegramUser.query.filter(TelegramUser.telegram_id.in_(demo_tg_ids)).delete(
        synchronize_session=False
    )
    Tariff.query.filter(Tariff.name.like(f"{DEMO_TARIFF_NAME_PREFIX}%")).delete(
        synchronize_session=False
    )
    db.session.commit()


def create_tariffs() -> list[Tariff]:
    print("→ Creating tariffs…")
    created: list[Tariff] = []
    for sort, (name, price, days, is_trial, visibility, items) in enumerate(TARIFFS):
        t = Tariff(
            name=f"{DEMO_TARIFF_NAME_PREFIX}{name}",
            price_rub=price,
            period_days=days,
            visibility=visibility,
            is_trial=is_trial,
            enabled=True,
            sort_order=sort,
        )
        db.session.add(t)
        db.session.flush()
        for item_sort, (tag, label, gb, groups) in enumerate(items):
            db.session.add(
                TariffItem(
                    tariff_id=t.id,
                    inbound_tag=tag,
                    label=label,
                    traffic_gb=gb,
                    allowed_node_groups=groups,
                    sort_order=item_sort,
                )
            )
        created.append(t)
    db.session.commit()
    return created


def create_telegram_users() -> list[TelegramUser]:
    print("→ Creating Telegram users…")
    now = datetime.utcnow()
    created: list[TelegramUser] = []
    for tg_id, username, lang, trial_days_ago, blocked in USERS:
        first_seen_days = random.randint(2, 200)
        last_seen_days = (
            random.randint(0, 90)
            if not blocked
            else random.randint(30, 200)
        )
        u = TelegramUser(
            telegram_id=tg_id,
            username=username,
            language=lang,
            trial_used_at=(now - timedelta(days=trial_days_ago)) if trial_days_ago else None,
            blocked=blocked,
            first_seen_at=now - timedelta(days=first_seen_days),
            last_seen_at=now - timedelta(days=last_seen_days),
        )
        db.session.add(u)
        created.append(u)
    db.session.commit()
    return created


def link_clients_to_users(tg_users: list[TelegramUser]):
    """Attach a TelegramUser to about 60% of the demo panel clients so the
    bot user-drawer in the panel UI shows real subscription state."""
    print("→ Linking existing demo Clients to Telegram users…")
    demo_clients = Client.query.filter(Client.inbound_tag.like(f"{DEMO_INBOUND_PREFIX}%")).all()
    available_users = [u.telegram_id for u in tg_users if not u.blocked]
    random.shuffle(available_users)
    pool_index = 0
    linked = 0
    for c in demo_clients:
        if random.random() < 0.4:
            continue  # 40% of clients stay unlinked (manually-added in panel)
        if pool_index >= len(available_users):
            break
        c.telegram_id = available_users[pool_index]
        pool_index += 1
        linked += 1
    db.session.commit()
    print(f"   {linked}/{len(demo_clients)} clients linked")


def create_access_grants(tariffs: list[Tariff], tg_users: list[TelegramUser]):
    """Random mix of free grants (admin-issued) and paid grants (renewed)."""
    print("→ Creating UserTariffAccess grants…")
    now = datetime.utcnow()
    paid_tariffs = [t for t in tariffs if not t.is_trial]
    grants_made = 0
    for u in tg_users:
        if u.blocked:
            continue
        if random.random() < 0.35:
            continue  # 35% never bought anything
        n_grants = random.choices([1, 2], weights=[0.8, 0.2])[0]
        chosen = random.sample(paid_tariffs, min(n_grants, len(paid_tariffs)))
        for t in chosen:
            is_free = random.random() < 0.25
            renewal_offset_days = random.randint(-15, t.period_days)
            db.session.add(
                UserTariffAccess(
                    telegram_id=u.telegram_id,
                    tariff_id=t.id,
                    billing="free" if is_free else "paid",
                    next_renewal_at=now + timedelta(days=renewal_offset_days),
                    note=(
                        f"{DEMO_NOTE_PREFIX} admin grant"
                        if is_free
                        else None
                    ),
                    created_at=now - timedelta(days=random.randint(1, 180)),
                )
            )
            grants_made += 1
    db.session.commit()
    print(f"   {grants_made} grants")


def create_payments(tariffs: list[Tariff], tg_users: list[TelegramUser]):
    """30 days of payment history with a realistic status mix."""
    print("→ Creating Payments…")
    now = datetime.utcnow()
    paid_tariffs = [t for t in tariffs if not t.is_trial]
    statuses_weighted = (
        ["succeeded"] * 70
        + ["pending"] * 8
        + ["cancelled"] * 12
        + ["failed"] * 10
    )
    rows_added = 0
    for u in tg_users:
        if u.blocked:
            continue
        n_payments = random.choices([0, 1, 2, 3, 5], weights=[20, 35, 25, 15, 5])[0]
        for _ in range(n_payments):
            t = random.choice(paid_tariffs)
            status = random.choice(statuses_weighted)
            created = now - timedelta(days=random.randint(0, 60), hours=random.randint(0, 23))
            paid_at = (
                created + timedelta(minutes=random.randint(1, 25))
                if status == "succeeded"
                else None
            )
            db.session.add(
                Payment(
                    yookassa_id=f"demo-{uuid.uuid4()}",
                    telegram_id=u.telegram_id,
                    tariff_id=t.id,
                    tariff_snapshot={
                        "id": t.id,
                        "name": t.name,
                        "price_rub": t.price_rub,
                        "period_days": t.period_days,
                    },
                    amount_rub=t.price_rub,
                    status=status,
                    confirmation_url=(
                        f"https://yoomoney.ru/checkout/payments/v2/contract?orderId=demo-{uuid.uuid4()}"
                        if status == "pending"
                        else None
                    ),
                    metadata_json={"source": "demo", "tariff_name": t.name},
                    created_at=created,
                    paid_at=paid_at,
                )
            )
            rows_added += 1
    db.session.commit()
    print(f"   {rows_added} payments")


def main():
    app = create_app()
    with app.app_context():
        wipe()
        tariffs = create_tariffs()
        tg_users = create_telegram_users()
        link_clients_to_users(tg_users)
        create_access_grants(tariffs, tg_users)
        create_payments(tariffs, tg_users)

        print("\n─── Bot seed complete ───")
        print(f"  Tariffs:                  {Tariff.query.count()}")
        print(f"  TariffItems:              {TariffItem.query.count()}")
        print(f"  TelegramUsers:            {TelegramUser.query.count()}")
        print(f"  UserTariffAccess grants:  {UserTariffAccess.query.count()}")
        print(f"  Payments:                 {Payment.query.count()}")

        # Payment status breakdown
        from sqlalchemy import func

        by_status = (
            db.session.query(Payment.status, func.count(), func.sum(Payment.amount_rub))
            .group_by(Payment.status)
            .all()
        )
        print("\n  Payment status breakdown:")
        for s, n, total in by_status:
            print(f"    {s:12} {n:5}  total: {total or 0}₽")


if __name__ == "__main__":
    main()
