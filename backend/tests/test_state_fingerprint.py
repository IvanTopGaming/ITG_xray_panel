from panel_core.models import Client, Inbound, Outbound, ProvisionReceipt, SystemSetting


def _seed(db_session):
    ib = Inbound(tag="in-1", port=443, protocol="vless", stream_settings="{}")
    db_session.session.add(ib)
    db_session.session.add(Outbound(tag="direct", protocol="freedom", settings="{}"))
    db_session.session.add(Client(id="uuid-1", email="a@b", inbound_tag="in-1", up=0, down=0, expiry_time=0))
    db_session.session.commit()


def test_editing_an_outbound_in_place_changes_the_fingerprint(app, db):
    from panel_core.services.state_fingerprint import compute_fingerprint

    _seed(db)
    before = compute_fingerprint()

    row = Outbound.query.filter_by(tag="direct").one()
    row.settings = '{"domainStrategy": "UseIP"}'
    db.session.commit()

    assert compute_fingerprint() != before, (
        "правка настроек существующего выхода не меняет ни количество строк, ни последний номер — "
        "если отпечаток её не заметит, изменение маршрутизации никогда не доедет до зеркала"
    )


def test_flipping_receipt_materialized_changes_the_fingerprint(app, db):
    from panel_core.services.state_fingerprint import compute_fingerprint

    _seed(db)
    receipt = ProvisionReceipt(
        idempotency_key="pay:1", inbound_tag="in-1", telegram_id=1, response_json="{}", materialized=False
    )
    db.session.add(receipt)
    db.session.commit()
    before = compute_fingerprint()

    receipt.materialized = True
    db.session.commit()

    assert compute_fingerprint() != before, (
        "materialized — единственное поле в append-only таблицах, которое меняется задним числом"
    )


def test_client_traffic_does_not_change_the_fingerprint(app, db):
    from panel_core.services.state_fingerprint import compute_fingerprint

    _seed(db)
    before = compute_fingerprint()

    client = db.session.get(Client, "uuid-1")
    client.up = 12345
    client.down = 67890
    db.session.commit()

    assert compute_fingerprint() == before, (
        "отрицательный контроль: трафик — горячая половина, она ездит каждые 10 секунд отдельно. "
        "Если он попадёт в отпечаток, мастер будет тянуть 200 КБ холодного состояния каждый опрос — "
        "то есть мы построим вариант, который сами же и отвергли"
    )


def test_only_allowlisted_settings_count(app, db):
    from panel_core.services.state_fingerprint import compute_fingerprint

    _seed(db)
    before = compute_fingerprint()

    db.session.add(SystemSetting(key="bot_token", value="123:secret"))
    db.session.commit()

    assert compute_fingerprint() == before, (
        "в зеркало едет поимённый список настроек, а не вся таблица — иначе туда затешется "
        "идентификатор экземпляра и служебные строки"
    )
