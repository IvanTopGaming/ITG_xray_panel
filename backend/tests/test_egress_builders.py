import pytest

from app.extensions import db
from app.models import Outbound
from app.services.egress import build_bind_ips, build_host_script


@pytest.fixture
def two_egress(app):
    with app.app_context():
        db.session.add_all(
            [
                Outbound(
                    tag="ded-1",
                    protocol="freedom",
                    settings="{}",
                    stream_settings="{}",
                    mux="{}",
                    send_through="172.28.0.128",
                    public_ip="203.0.113.7",
                ),
                Outbound(
                    tag="ded-2",
                    protocol="freedom",
                    settings="{}",
                    stream_settings="{}",
                    mux="{}",
                    send_through="172.28.0.129",
                    public_ip="198.51.100.9",
                    gateway="198.51.100.1",
                ),
                Outbound(
                    tag="plain",
                    protocol="freedom",
                    settings="{}",
                    stream_settings="{}",
                    mux="{}",
                ),
            ]
        )
        db.session.commit()
        yield


def test_build_bind_ips_lists_only_egress(app, two_egress):
    with app.app_context():
        rows = build_bind_ips()
    ips = sorted(r["send_through"] for r in rows)
    assert ips == ["172.28.0.128", "172.28.0.129"]
    assert all(r["prefix"] == 24 for r in rows)


def test_host_script_is_idempotent_and_self_cleaning(app, two_egress):
    with app.app_context():
        script = build_host_script("eth0")
    assert "iptables -t nat -N EGRESS_SNAT" in script
    assert "iptables -t nat -F EGRESS_SNAT" in script
    assert "-s 172.28.0.128 -j SNAT --to-source 203.0.113.7" in script
    assert "ip addr add 203.0.113.7/32 dev eth0" in script
    assert "table" in script and "198.51.100.1" in script
    assert "plain" not in script


@pytest.mark.parametrize("bad_iface", ["eth0;rm -rf /", "bad iface"])
def test_host_script_rejects_bad_iface(app, two_egress, bad_iface):
    with app.app_context():
        with pytest.raises(ValueError):
            build_host_script(bad_iface)


def test_host_script_excludes_corrupt_ip_row(app):
    with app.app_context():
        db.session.add_all(
            [
                Outbound(
                    tag="corrupt",
                    protocol="freedom",
                    settings="{}",
                    stream_settings="{}",
                    mux="{}",
                    send_through="172.28.0.130",
                    public_ip="1.2.3.4; curl evil",
                ),
                Outbound(
                    tag="good",
                    protocol="freedom",
                    settings="{}",
                    stream_settings="{}",
                    mux="{}",
                    send_through="172.28.0.131",
                    public_ip="203.0.113.50",
                ),
            ]
        )
        db.session.commit()
        script = build_host_script("eth0")
    assert "1.2.3.4; curl evil" not in script
    assert "curl evil" not in script
    assert "203.0.113.50" in script


def test_build_bind_ips_excludes_corrupt_send_through(app):
    with app.app_context():
        db.session.add_all(
            [
                Outbound(
                    tag="corrupt",
                    protocol="freedom",
                    settings="{}",
                    stream_settings="{}",
                    mux="{}",
                    send_through='172.28.0.130"; rm -rf /',
                    public_ip="203.0.113.60",
                ),
                Outbound(
                    tag="good",
                    protocol="freedom",
                    settings="{}",
                    stream_settings="{}",
                    mux="{}",
                    send_through="172.28.0.131",
                    public_ip="203.0.113.61",
                ),
            ]
        )
        db.session.commit()
        rows = build_bind_ips()
    ips = [r["send_through"] for r in rows]
    assert ips == ["172.28.0.131"]
