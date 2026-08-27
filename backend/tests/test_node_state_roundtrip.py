import pytest
from sqlalchemy import text

from panel_core.models import (
    Admin,
    Balancer,
    Client,
    FederationConfig,
    Inbound,
    NotificationLog,
    Outbound,
    ProvisionReceipt,
    RoutingProfile,
)

MIRRORED_TABLES = (Inbound, Client, Outbound, RoutingProfile, Balancer, ProvisionReceipt, NotificationLog)


def _dump(model):
    from panel_core.services.state_export import MIRROR_EXCLUDED_COLUMNS

    skip = MIRROR_EXCLUDED_COLUMNS.get(model.__name__, frozenset())
    columns = [c.name for c in model.__table__.columns if c.name not in skip]
    rows = [tuple(getattr(row, name) for name in columns) for row in model.query.all()]
    return sorted(rows, key=lambda r: tuple(str(x) for x in r))


def test_state_survives_a_round_trip(app, db, rich_node):
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_cold_state, export_hot_state

    before = {model.__name__: _dump(model) for model in MIRRORED_TABLES}
    hot, cold = export_hot_state(), export_cold_state()

    for model in MIRRORED_TABLES:
        model.query.delete()
    db.session.commit()

    apply_state(hot, cold, carry_admin=False)

    after = {model.__name__: _dump(model) for model in MIRRORED_TABLES}

    for name in before:
        if name == "Outbound":
            continue
        assert after[name] == before[name], (
            f"таблица {name} не пережила круговой прогон поле в поле. Характерный способ сломать "
            f"эту фичу — поле, которое молча не доехало: ошибки нет, лога нет, обнаруживается "
            f"через полгода в момент аварии"
        )


def test_egress_arrives_disabled_with_no_public_ip(app, db, rich_node):
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_cold_state, export_hot_state

    hot, cold = export_hot_state(), export_cold_state()
    for model in MIRRORED_TABLES:
        model.query.delete()
    db.session.commit()

    apply_state(hot, cold, carry_admin=False)

    egress = Outbound.query.filter_by(tag="egress-1").one()
    assert egress.enable is False
    assert not egress.public_ip and not egress.gateway, (
        "публичные адреса у нового сервера другие; оставить старые — это не деградация, а чёрная "
        "дыра: SNAT на адрес, которого у машины нет, провайдер выбросит как спуфинг"
    )
    assert egress.send_through == "172.28.0.130", "внутренний адрес и привязка клиентов должны уцелеть"
    assert db.session.get(Client, "uuid-1").preferred_outbound == "egress-1"


def test_host_plan_is_empty_after_restore(app, db, rich_node):
    from panel_core.services.egress import build_host_plan
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_cold_state, export_hot_state

    hot, cold = export_hot_state(), export_cold_state()
    for model in MIRRORED_TABLES:
        model.query.delete()
    db.session.commit()

    apply_state(hot, cold, carry_admin=False)

    assert build_host_plan() == [], (
        "хостовый скрипт берёт план у панели: пустой план он отрабатывает как «вычистить хост», "
        "и именно это доказывает, что трафик не уйдёт в чёрную дыру"
    )


def test_defaults_are_upserted_by_tag_not_duplicated(app, db, rich_node):
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_cold_state, export_hot_state

    hot, cold = export_hot_state(), export_cold_state()

    apply_state(hot, cold, carry_admin=False)

    assert Outbound.query.filter_by(tag="direct").count() == 1, (
        "нода создаёт direct и block сама при старте, до раскладки — класть их надо поверх по тегу"
    )


def test_federation_config_and_instance_id_are_untouched(app, db, rich_node):
    from panel_core.services.node_identity import get_or_create_instance_id
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_cold_state, export_hot_state

    hot, cold = export_hot_state(), export_cold_state()

    config = db.session.get(FederationConfig, 1) or FederationConfig(id=1)
    config.federation_token = "fresh"
    config.master_url = "https://master"
    db.session.add(config)
    db.session.commit()
    fresh_instance = get_or_create_instance_id()

    apply_state(hot, cold, carry_admin=False)

    assert db.session.get(FederationConfig, 1).federation_token == "fresh"
    assert get_or_create_instance_id() == fresh_instance


def test_admin_travels_only_when_asked(app, db, rich_node):
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_cold_state, export_hot_state

    db.session.add(Admin(username="olduser", password="scrypt:hash:from-old-box", password_changed_at=1700000000))
    db.session.commit()
    hot, cold = export_hot_state(), export_cold_state()

    Admin.query.delete()
    db.session.add(Admin(username="admin", password="scrypt:hash:from-env", password_changed_at=1800000000))
    db.session.commit()

    apply_state(hot, cold, carry_admin=False)
    assert Admin.query.one().password == "scrypt:hash:from-env"

    apply_state(hot, cold, carry_admin=True)
    carried = Admin.query.one()
    assert carried.password == "scrypt:hash:from-old-box"
    assert carried.password_changed_at == 1700000000, (
        "от отметки смены пароля зависит признак версии в выданных токенах: потеряешь — сессии "
        "либо не протухнут когда должны, либо протухнут когда не должны"
    )


def test_a_failed_apply_leaves_nothing_behind(app, db, rich_node):
    from panel_core.services import state_apply
    from panel_core.services.state_export import export_cold_state, export_hot_state

    hot, cold = export_hot_state(), export_cold_state()
    for model in MIRRORED_TABLES:
        model.query.delete()
    db.session.commit()

    cold["receipts"].append(
        {
            "idempotency_key": None,
            "inbound_tag": None,
            "telegram_id": None,
            "response_json": None,
            "materialized": False,
        }
    )

    with pytest.raises(Exception):
        state_apply.apply_state(hot, cold, carry_admin=False)
    db.session.rollback()

    assert Inbound.query.count() == 0, (
        "раскладка идёт одной транзакцией: половина состояния хуже, чем ничего, потому что "
        "выглядит целой и таковой не является"
    )


def test_a_damaged_null_expiry_time_survives_as_null_not_zero(app, db, rich_node):
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_cold_state, export_hot_state

    db.session.execute(text("UPDATE client SET expiry_time = NULL WHERE id = 'uuid-1'"))
    db.session.commit()

    hot, cold = export_hot_state(), export_cold_state()
    exported = next(c for ib in hot["inbounds"] for c in ib["clients"] if c["id"] == "uuid-1")
    assert exported["expiry_time"] is None

    for model in MIRRORED_TABLES:
        model.query.delete()
    db.session.commit()

    apply_state(hot, cold, carry_admin=False)

    assert db.session.get(Client, "uuid-1").expiry_time is None, (
        "expiry_time == 0 значит «никогда» и сохраняется дословно; NULL значит «повреждённая "
        "строка», которую считают от текущего момента. Превратить NULL в 0 — значит молча выдать "
        "вечный доступ повреждённой строке"
    )


def test_a_stale_outbound_does_not_survive_the_repair(app, db, rich_node):
    from panel_core.services.egress import build_host_plan
    from panel_core.services.state_apply import apply_state
    from panel_core.services.state_export import export_cold_state, export_hot_state

    hot, cold = export_hot_state(), export_cold_state()

    db.session.add(
        Outbound(
            tag="stale-egress",
            protocol="freedom",
            settings="{}",
            enable=True,
            send_through="172.28.0.199",
            public_ip="198.51.100.9",
            gateway="198.51.100.1",
        )
    )
    db.session.commit()

    apply_state(hot, cold, carry_admin=False)

    assert Outbound.query.filter_by(tag="stale-egress").first() is None, (
        "раскладка обещает замену, а не слияние: незеркальный выход не должен пережить ремонт"
    )
    assert build_host_plan() == [], (
        "переживший ремонт выход со старым публичным адресом попадает в план хоста и провоцирует "
        "SNAT на адрес, которого у машины больше нет"
    )


def test_a_missing_cold_outbounds_key_leaves_the_bootstrap_defaults_alone(app, db):
    from panel_core.services.state_apply import apply_state

    db.session.add_all(
        [
            Outbound(tag="direct", protocol="freedom", settings="{}", enable=True),
            Outbound(tag="block", protocol="blackhole", settings="{}", enable=True),
        ]
    )
    db.session.commit()

    apply_state({}, {}, carry_admin=False)

    assert {o.tag for o in Outbound.query.all()} == {"direct", "block"}, (
        "нода старого релиза без cold_fingerprint никогда не попадала в зеркало: claim приносит "
        "cold={} без ключа 'outbounds' вовсе, а это не то же самое, что 'ноль исходящих' — "
        "NOT IN () на пустом множестве истинен для всех строк и сносит то, что bootstrap_defaults "
        "только что сам создал"
    )
