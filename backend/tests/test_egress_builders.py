import pytest

from panel_core.extensions import db
from panel_core.models import Outbound
from panel_core.services.egress import build_bind_ips


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


def test_host_script_builder_is_gone():
    """§8.11: host-level egress setup left the panel; a future installer owns it.

    Pinned so the builder cannot quietly come back with the route (see
    tests/test_api_egress.py for the HTTP half of this guard).
    """
    import panel_core.services.egress as egress

    assert not hasattr(egress, "build_host_script")
    assert not hasattr(egress, "DEFAULT_UPLINK_IFACE")
    assert hasattr(egress, "build_bind_ips"), "the sidecar endpoint must survive the removal"
