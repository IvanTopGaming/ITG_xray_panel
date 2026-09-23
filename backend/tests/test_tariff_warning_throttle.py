import logging
from types import SimpleNamespace


def test_repeated_undeliverable_tariff_warning_is_bounded(monkeypatch, caplog):
    from panel_core.services import tariff_delivery

    tariff = SimpleNamespace(id=998123, name="broken", items=[])
    now = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            tariff_delivery.log_undeliverable(tariff, "catalog")
        assert len(caplog.records) == 1
        now[0] += 301
        tariff_delivery.log_undeliverable(tariff, "catalog")
    assert len(caplog.records) == 2
