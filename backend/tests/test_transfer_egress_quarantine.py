import json
from pathlib import Path

import pytest

from panel_core.models import Balancer, Client, Inbound, Outbound, RoutingProfile, SystemSetting
from panel_core.services.state_apply import apply_state
from panel_core.services.state_export import export_cold_state, export_hot_state
from panel_core.xray import engine


@pytest.fixture
def transferred(db):
    profile = RoutingProfile(name="dedicated", rules='[{"domain":["full:private.example"],"outboundTag":"egress"}]')
    db.session.add(profile)
    db.session.flush()
    db.session.add_all(
        [
            Outbound(tag="direct", protocol="freedom"),
            Outbound(tag="block", protocol="blackhole"),
            Outbound(
                tag="egress",
                protocol="freedom",
                public_ip="203.0.113.8",
                gateway="203.0.113.1",
                send_through="172.28.0.130",
            ),
            Inbound(
                tag="users",
                port=10443,
                protocol="vless",
                stream_settings='{"network":"tcp","security":"none"}',
                routing_profile_id=profile.id,
            ),
        ]
    )
    db.session.flush()
    db.session.add_all(
        [
            Client(
                id="11111111-1111-4111-8111-111111111111",
                email="dedicated",
                inbound_tag="users",
                preferred_outbound="egress",
                enable=True,
            ),
            Client(
                id="22222222-2222-4222-8222-222222222222",
                email="ordinary",
                inbound_tag="users",
                preferred_outbound="direct",
                enable=True,
            ),
        ]
    )
    db.session.commit()
    apply_state(export_hot_state(), export_cold_state(), carry_admin=False)


def _config():
    engine.generate_config_file(validate=False)
    return json.loads(Path(engine.CONFIG_PATH).read_text())


def test_transfer_blocks_dedicated_routes_without_stopping_other_users(db, transferred):
    config = _config()
    outbounds = {outbound["tag"]: outbound for outbound in config["outbounds"]}
    assert outbounds["egress"] == {"tag": "egress", "protocol": "blackhole", "settings": {}}
    assert outbounds["direct"]["protocol"] == "freedom"
    assert config["outbounds"][0]["tag"] == "direct"
    rules = config["routing"]["rules"]
    assert any(rule.get("domain") == ["full:private.example"] and rule.get("outboundTag") == "egress" for rule in rules)
    assert any(rule.get("user") == ["v1|users|ZGVkaWNhdGVk"] and rule.get("outboundTag") == "egress" for rule in rules)
    assert any(rule.get("user") == ["v1|users|b3JkaW5hcnk"] and rule.get("outboundTag") == "direct" for rule in rules)
    assert Outbound.query.filter_by(tag="egress").one().protocol == "freedom"
    assert Client.query.filter_by(email="dedicated").one().preferred_outbound == "egress"


@pytest.mark.parametrize("target_key", ["outboundTag", "balancerTag"])
@pytest.mark.parametrize(
    "selector,fallback", [('["egress"]', "direct"), ('["egress","direct"]', None), ('["direct"]', "egress")]
)
def test_transfer_blocks_whole_dependent_balancer_including_public_fallback(
    db, transferred, selector, fallback, target_key
):
    db.session.add(Balancer(tag="pool", selector=selector, fallback_tag=fallback))
    Client.query.filter_by(email="dedicated").one().preferred_outbound = "pool"
    RoutingProfile.query.one().rules = json.dumps([{target_key: "pool", "domain": ["full:private.example"]}])
    db.session.commit()
    config = _config()
    assert {item["tag"]: item["protocol"] for item in config["outbounds"]}["pool"] == "blackhole"
    assert all(item["tag"] != "pool" for item in config["routing"]["balancers"])
    assert any(rule.get("outboundTag") == "pool" and "user" in rule for rule in config["routing"]["rules"])
    assert any(rule.get("outboundTag") == "pool" and "domain" in rule for rule in config["routing"]["rules"])
    assert Balancer.query.one().selector == selector
    assert Balancer.query.one().fallback_tag == fallback


def test_quarantine_survives_export_and_another_transfer(db, transferred):
    hot, cold = export_hot_state(), export_cold_state()
    SystemSetting.query.delete()
    db.session.commit()
    apply_state(hot, cold, carry_admin=False)
    config = _config()
    assert next(item for item in config["outbounds"] if item["tag"] == "egress")["protocol"] == "blackhole"


def test_balancer_prefix_cannot_route_around_quarantined_outbound(db, transferred):
    db.session.add_all(
        [
            Outbound(tag="eg", protocol="freedom"),
            Balancer(tag="pool", selector='["eg"]', fallback_tag="direct"),
        ]
    )
    Client.query.filter_by(email="dedicated").one().preferred_outbound = "pool"
    db.session.commit()
    config = _config()
    assert next(item for item in config["outbounds"] if item["tag"] == "pool")["protocol"] == "blackhole"
    assert all(item["tag"] != "pool" for item in config["routing"]["balancers"])


def test_balancer_quarantine_reaches_prefix_dependents_to_a_fixpoint(db, transferred):
    db.session.add_all(
        [
            Outbound(tag="eg", protocol="freedom"),
            Outbound(tag="pool", protocol="freedom"),
            Balancer(tag="outer", selector='["pool"]', fallback_tag="direct"),
            Balancer(tag="pool-quarantined", selector='["eg"]', fallback_tag="direct"),
            Balancer(tag="unrelated-pool", selector='["direct"]'),
        ]
    )
    Client.query.filter_by(email="dedicated").one().preferred_outbound = "outer"
    db.session.commit()
    config = _config()
    protocols = {item["tag"]: item["protocol"] for item in config["outbounds"]}
    assert protocols["pool-quarantined"] == "blackhole"
    assert protocols["outer"] == "blackhole"
    assert {item["tag"] for item in config["routing"]["balancers"]} == {"unrelated-pool"}


def test_unrelated_disabled_route_is_still_rejected(db, transferred):
    db.session.add(Outbound(tag="unrelated", protocol="freedom", enable=False))
    Client.query.filter_by(email="ordinary").one().preferred_outbound = "unrelated"
    db.session.commit()
    with pytest.raises(ValueError, match="Unknown or disabled preferred routing target: unrelated"):
        _config()


@pytest.fixture
def outbound_api(app, db, transferred, monkeypatch):
    from panel_core.api.outbound import bp
    from panel_core.models import FederationConfig

    app.register_blueprint(bp, url_prefix="/api")
    db.session.get(FederationConfig, 1).federation_token = "test-federation"
    db.session.commit()
    monkeypatch.setattr(engine, "_validate_xray_config", lambda path: None)
    monkeypatch.setattr("panel_core.api.outbound.restart_xray_container", lambda: None)
    return app.test_client()


def _update(api, payload):
    return api.put("/api/outbounds/egress", headers={"X-Federation-Token": "test-federation"}, json=payload)


def test_enabling_without_configuring_an_address_keeps_quarantine(db, outbound_api):
    response = _update(outbound_api, {"enable": True})
    assert response.status_code == 400
    assert "public_ip" in response.get_json()["error"]
    assert Outbound.query.filter_by(tag="egress").one().enable is False
    assert next(item for item in _config()["outbounds"] if item["tag"] == "egress")["protocol"] == "blackhole"


@pytest.mark.parametrize("public_ip", ["203.0.113.25", ""])
def test_explicit_reconfiguration_releases_routes(db, outbound_api, public_ip):
    response = _update(outbound_api, {"enable": True, "public_ip": public_ip})
    assert response.status_code == 200, response.get_json()
    config = _config()
    outbound = next(item for item in config["outbounds"] if item["tag"] == "egress")
    assert outbound["protocol"] == "freedom"
    assert bool(outbound.get("sendThrough")) is bool(public_ip)
    assert not any(row["key"] == "transfer_pending_egress" for row in export_cold_state()["settings"])


def test_configuring_an_address_while_disabled_keeps_quarantine(db, outbound_api):
    response = _update(outbound_api, {"public_ip": "203.0.113.25"})
    assert response.status_code == 200, response.get_json()
    assert next(item for item in _config()["outbounds"] if item["tag"] == "egress")["protocol"] == "blackhole"
    response = _update(outbound_api, {"enable": True})
    assert response.status_code == 200, response.get_json()
    assert next(item for item in _config()["outbounds"] if item["tag"] == "egress")["protocol"] == "freedom"


def test_saving_removed_dedicated_address_allows_separate_enable(db, outbound_api):
    response = _update(outbound_api, {"public_ip": ""})
    assert response.status_code == 200, response.get_json()
    outbound = Outbound.query.filter_by(tag="egress").one()
    assert outbound.enable is False
    assert outbound.send_through is None
    assert next(item for item in _config()["outbounds"] if item["tag"] == "egress")["protocol"] == "blackhole"
    db.session.remove()

    response = _update(outbound_api, {"enable": True})
    assert response.status_code == 200, response.get_json()
    outbound = next(item for item in _config()["outbounds"] if item["tag"] == "egress")
    assert outbound["protocol"] == "freedom"
    assert "sendThrough" not in outbound
    assert not any(row["key"] == "transfer_pending_egress" for row in export_cold_state()["settings"])


def test_gateway_only_edit_cannot_implicitly_remove_transferred_binding(db, outbound_api):
    response = _update(outbound_api, {"gateway": "203.0.113.1"})
    assert response.status_code == 400, response.get_json()
    assert Outbound.query.filter_by(tag="egress").one().send_through == "172.28.0.130"
    response = _update(outbound_api, {"enable": True})
    assert response.status_code == 400, response.get_json()
    assert next(item for item in _config()["outbounds"] if item["tag"] == "egress")["protocol"] == "blackhole"


def test_removing_quarantined_outbound_cannot_quarantine_a_reused_tag(db, outbound_api):
    RoutingProfile.query.one().rules = "[]"
    db.session.commit()
    response = outbound_api.delete("/api/outbounds/egress", headers={"X-Federation-Token": "test-federation"})
    assert response.status_code == 200, response.get_json()
    assert not any(row["key"] == "transfer_pending_egress" for row in export_cold_state()["settings"])
    db.session.add(Outbound(tag="egress", protocol="freedom"))
    db.session.commit()
    assert next(item for item in _config()["outbounds"] if item["tag"] == "egress")["protocol"] == "freedom"


def test_outbound_list_reports_quarantine_until_address_is_enabled(db, outbound_api):
    response = _update(outbound_api, {"public_ip": "203.0.113.25"})
    assert response.status_code == 200
    rows = outbound_api.get("/api/outbounds", headers={"X-Federation-Token": "test-federation"}).get_json()
    assert next(row for row in rows if row["tag"] == "egress")["transfer_pending"] is True
    assert next(row for row in rows if row["tag"] == "direct")["transfer_pending"] is False


def test_replacing_state_does_not_inherit_destination_quarantine(db, transferred):
    hot, cold = export_hot_state(), export_cold_state()
    cold["settings"] = [row for row in cold["settings"] if row["key"] != "transfer_pending_egress"]
    outbound = next(row for row in cold["outbounds"] if row["tag"] == "egress")
    outbound["enable"] = True
    outbound["send_through"] = None
    apply_state(hot, cold, carry_admin=False)
    assert next(item for item in _config()["outbounds"] if item["tag"] == "egress")["protocol"] == "freedom"
