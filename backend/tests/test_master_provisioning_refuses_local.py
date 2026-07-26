import json

import pytest

from panel_core.xray import gateway as gw
from panel_core.xray.local import LocalXrayGateway

PROTOCOLS = ["vless", "trojan"]


def _reset_scheduler():
    from panel_core.extensions import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


@pytest.fixture(autouse=True)
def _scheduler_teardown():
    yield
    _reset_scheduler()


@pytest.fixture
def master_app(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/master-provision.db")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    from panel_core.roles import master

    return master.create_app()


def _seed_local_tariff(protocol, port):
    from panel_core.extensions import db
    from panel_core.models import Inbound, Tariff, TariffItem

    db.session.add(
        Inbound(
            tag="local-tag",
            port=port,
            protocol=protocol,
            stream_settings=json.dumps({"network": "tcp", "security": "none"}),
        )
    )
    tariff = Tariff(name="Local Only", price_rub=100, period_days=30)
    db.session.add(tariff)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=tariff.id, inbound_tag="local-tag", traffic_gb=10, panel_id=None))
    db.session.commit()
    return tariff


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_master_refuses_to_provision_a_local_tariff_item(master_app, protocol):
    from panel_core.extensions import db
    from panel_core.models import Client
    from panel_core.services import provisioning

    with master_app.app_context():
        tariff = _seed_local_tariff(protocol, 20000 + len(protocol))

        with pytest.raises(gw.LocalXrayUnavailable) as excinfo:
            provisioning.apply_tariff_for_user(4242, tariff, source="test")

        message = str(excinfo.value)
        assert "Local Only" in message
        assert "local-tag" in message
        assert "panel_id" in message

        db.session.rollback()
        assert Client.query.filter_by(telegram_id=4242).count() == 0


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_master_refuses_single_item_provisioning(master_app, protocol):
    from panel_core.extensions import db
    from panel_core.models import Client
    from panel_core.services import provisioning

    with master_app.app_context():
        _seed_local_tariff(protocol, 21000 + len(protocol))

        with pytest.raises(gw.LocalXrayUnavailable):
            provisioning.provision_single_item(
                telegram_id=4243,
                inbound_tag="local-tag",
                expiry_ms=0,
                limit_bytes=0,
            )

        db.session.rollback()
        assert Client.query.filter_by(telegram_id=4243).count() == 0


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_master_refuses_to_extend_an_existing_local_client(master_app, protocol):
    from panel_core.extensions import db
    from panel_core.models import Client
    from panel_core.services import provisioning

    with master_app.app_context():
        tariff = _seed_local_tariff(protocol, 22000 + len(protocol))
        db.session.add(
            Client(
                id="existing-identity",
                email="tg4244_local-tag",
                inbound_tag="local-tag",
                telegram_id=4244,
                tariff_id=tariff.id,
                expiry_time=1,
                limit_bytes=1,
                enable=False,
            )
        )
        db.session.commit()

        with pytest.raises(gw.LocalXrayUnavailable):
            provisioning.apply_tariff_for_user(4244, tariff, source="test")

        db.session.rollback()
        untouched = Client.query.filter_by(telegram_id=4244).one()
        assert untouched.expiry_time == 1
        assert untouched.limit_bytes == 1
        assert untouched.enable is False


def test_worker_still_provisions_the_same_tariff(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "worker")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/worker-provision.db")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    from panel_core.roles import worker

    app = worker.create_app()

    from panel_core.extensions import db
    from panel_core.models import Client
    from panel_core.services import provisioning

    with app.app_context():
        tariff = _seed_local_tariff("trojan", 23000)

        calls = []

        class _Recording(LocalXrayGateway):
            def apply_config(self, validate=True):
                calls.append("apply_config")

            def restart(self):
                calls.append("restart")

        gw.set_xray_gateway(_Recording())

        provisioning.apply_tariff_for_user(4245, tariff, source="test")

        assert Client.query.filter_by(telegram_id=4245).count() == 1
        assert calls == ["apply_config", "restart"]
        db.session.remove()
