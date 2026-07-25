import pytest

from panel_core.services.egress import allocate_bind_ip, get_bind_prefix, get_pool_range


def test_pool_defaults():
    start, end = get_pool_range()
    assert str(start) == "172.28.0.128"
    assert str(end) == "172.28.0.254"
    assert get_bind_prefix() == 24


def test_allocate_picks_lowest_free():
    assert allocate_bind_ip([]) == "172.28.0.128"
    assert allocate_bind_ip(["172.28.0.128"]) == "172.28.0.129"
    assert allocate_bind_ip(["172.28.0.128", "172.28.0.130"]) == "172.28.0.129"


def test_allocate_ignores_blanks():
    assert allocate_bind_ip([None, "", "172.28.0.128"]) == "172.28.0.129"


def test_allocate_raises_when_exhausted(monkeypatch):
    monkeypatch.setenv("EGRESS_BIND_POOL_RANGE", "172.28.0.240-172.28.0.241")
    with pytest.raises(ValueError):
        allocate_bind_ip(["172.28.0.240", "172.28.0.241"])
