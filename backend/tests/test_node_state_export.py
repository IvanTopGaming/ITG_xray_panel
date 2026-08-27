from panel_core.models import Client, Inbound, Outbound, ProvisionReceipt, RoutingProfile, SystemSetting


def _register_federation(app):
    from panel_core.api import federation

    if not any(bp.name == "federation" for bp in app.blueprints.values()):
        app.register_blueprint(federation.bp, url_prefix="/api")


def _seed_rich(db):
    profile = RoutingProfile(name="ru", rules='[{"type":"field"}]', enable=True)
    db.session.add(profile)
    db.session.flush()

    db.session.add(
        Inbound(
            tag="in-reality",
            port=443,
            protocol="vless",
            stream_settings='{"security":"reality","realitySettings":{"privateKey":"SECRET-KEY","serverNames":["www.google.com"]}}',
            routing_profile_id=profile.id,
            label="основной",
        )
    )
    db.session.add(
        Outbound(
            tag="egress-1",
            protocol="freedom",
            settings="{}",
            enable=True,
            send_through="172.28.0.130",
            public_ip="203.0.113.7",
            gateway="203.0.113.1",
        )
    )
    db.session.add(
        Client(
            id="uuid-1",
            email="a@b",
            inbound_tag="in-reality",
            up=10,
            down=20,
            expiry_time=0,
            last_reset_time=1750000000000,
            source_ips='["1.2.3.4"]',
            telegram_id=777,
            tariff_id=3,
            preferred_outbound="egress-1",
        )
    )
    db.session.add(SystemSetting(key="xray_log_level", value="warning"))
    db.session.add(SystemSetting(key="bot_token", value="123:secret"))
    db.session.add(
        ProvisionReceipt(
            idempotency_key="pay:1", inbound_tag="in-reality", telegram_id=777, response_json="{}", materialized=True
        )
    )
    db.session.commit()


def test_hot_state_carries_the_private_key_and_the_uuid(app, db):
    from panel_core.services.state_export import export_hot_state

    _seed_rich(db)
    hot = export_hot_state()

    inbound = hot["inbounds"][0]
    assert "SECRET-KEY" in inbound["stream_settings"]["realitySettings"]["privateKey"], (
        "без приватного ключа REALITY восстановленная нода — другой сервер, и всем придётся "
        "переимпортировать конфиги; ради того, чтобы ключ пережил смерть машины, всё и затевается"
    )
    client = inbound["clients"][0]
    assert client["id"] == "uuid-1"
    assert client["tariff_id"] == 3, "tariff_id ссылается на тариф в базе мастера и обязан совпасть"


def test_hot_state_carries_last_reset_time_and_source_ips(app, db):
    from panel_core.services.state_export import export_hot_state

    _seed_rich(db)
    client = export_hot_state()["inbounds"][0]["clients"][0]

    assert client["last_reset_time"] == 1750000000000, (
        "без отметки последнего обнуления восстановленная нода может обнулить трафик второй раз за месяц"
    )
    assert client["source_ips"] == '["1.2.3.4"]'


def test_cold_state_carries_egress_verbatim(app, db):
    from panel_core.services.state_export import export_cold_state

    _seed_rich(db)
    outbound = export_cold_state()["outbounds"][0]

    assert outbound["public_ip"] == "203.0.113.7", (
        "выгрузка отдаёт как есть; гасит публичный адрес раскладка, а не выгрузка — "
        "иначе зеркало потеряет информацию о том, что выход вообще был выделенным"
    )
    assert outbound["send_through"] == "172.28.0.130"


def test_cold_state_settings_are_allowlisted(app, db):
    from panel_core.services.state_export import export_cold_state

    _seed_rich(db)
    keys = {row["key"] for row in export_cold_state()["settings"]}

    assert keys == {"xray_log_level"}
    assert "bot_token" not in keys, (
        "в зеркало едет поимённый список, иначе туда затешется старый идентификатор экземпляра "
        "и мы воскресим ровно то, что пытались не воскрешать"
    )


def test_state_endpoint_needs_a_federation_token(app, db):
    _register_federation(app)
    _seed_rich(db)
    client = app.test_client()

    assert client.get("/api/federation/state").status_code == 401


def test_state_endpoint_returns_both_halves(app, db):
    from panel_core.models import FederationConfig

    _register_federation(app)
    _seed_rich(db)
    cfg = db.session.get(FederationConfig, 1)
    cfg.federation_token = "fed-token"
    db.session.commit()

    resp = app.test_client().get("/api/federation/state", headers={"X-Federation-Token": "fed-token"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) >= {"hot", "cold", "fingerprint", "instance_id", "app_version"}
    assert len(body["fingerprint"]) == 64
