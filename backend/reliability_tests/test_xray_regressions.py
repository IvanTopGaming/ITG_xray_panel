import inspect
import base64
import json
import uuid

import pytest
from flask import Flask

from panel_core.api import inbound, outbound
from panel_core.extensions import db
from panel_core.models import Balancer, Client, Inbound, Outbound, RoutingProfile
from panel_core.xray import protocol


@pytest.fixture
def app(monkeypatch, tmp_path):
    from panel_core.services import runtime_apply

    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True, XRAY_CONFIG_LOCK_PATH=str(tmp_path / "config.lock")
    )
    db.init_app(app)
    for module in (inbound, outbound):
        monkeypatch.setattr(module, "has_local_xray", lambda: True)
        monkeypatch.setattr(module, "restart_xray_container", lambda: None)
    monkeypatch.setattr(runtime_apply, "generate_config_file", lambda **kwargs: None)
    monkeypatch.setattr(runtime_apply, "restart_xray_container", lambda: inbound.restart_xray_container())
    monkeypatch.setattr(inbound.sub_cache, "invalidate_user", lambda *a: None)
    with app.app_context():
        db.create_all()
        db.session.add(Inbound(tag="ib", port=12345, protocol="vless", stream_settings="{}"))
        db.session.add(Outbound(tag="direct"))
        db.session.add(Outbound(tag="relay"))
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


def call(app, handler, data=None, *args, method="POST"):
    with app.test_request_context("/", json=data, method=method):
        result = inspect.unwrap(handler)(*args)
        if isinstance(result, tuple):
            return result[0].get_json(), result[1]
        return result.get_json(), result.status_code


def add_client(**values):
    client = Client(id=str(uuid.uuid4()), inbound_tag="ib", email="old", **values)
    db.session.add(client)
    db.session.commit()
    return client


def test_disabled_creation_never_adds_runtime_user(app, monkeypatch):
    active = set()
    monkeypatch.setattr(inbound, "_api_add_user_grpc", lambda tag, c: active.add(c.id) or True)
    body, status = call(app, inbound.add_user, {"email": "disabled", "enable": False}, "ib")
    assert status == 201
    assert body["enable"] is False
    assert active == set()


def test_renaming_routed_user_reloads_router(app, monkeypatch):
    client = add_client(enable=True, preferred_outbound="block")
    runtime_rules = {"old": "block"}
    monkeypatch.setattr(inbound, "_api_remove_user_grpc", lambda *a: True)
    monkeypatch.setattr(inbound, "_api_add_user_grpc", lambda *a: True)

    def restart():
        runtime_rules.clear()
        runtime_rules[client.email] = client.preferred_outbound

    monkeypatch.setattr(inbound, "restart_xray_container", restart)
    body, status = call(app, inbound.update_user, {"old_email": "old", "new_email": "renamed"}, "ib")
    assert status == 200
    assert runtime_rules == {"renamed": "block"}


def test_enabling_routed_user_reloads_router(app, monkeypatch):
    add_client(enable=False, preferred_outbound="block")
    runtime_rules = {}
    monkeypatch.setattr(inbound, "_api_add_user_grpc", lambda *a: True)
    monkeypatch.setattr(inbound, "restart_xray_container", lambda: runtime_rules.update(old="block"))
    body, status = call(
        app, inbound.bulk_enable_users_route, {"users": [{"tag": "ib", "email": "old"}], "enable": True}
    )
    assert status == 200
    assert runtime_rules == {"old": "block"}


def test_subtract_cannot_turn_finite_expiry_into_unlimited(app):
    client = add_client(expiry_time=1000)
    body, status = call(
        app,
        inbound.bulk_adjust_days_route,
        {"users": [{"tag": "ib", "email": "old"}], "days": 30000, "mode": "subtract"},
    )
    assert status == 200
    assert client.expiry_time == 1


@pytest.mark.parametrize("tag", ["direct", "api", "relay"])
def test_balancer_cannot_shadow_an_outbound(app, tag):
    body, status = call(app, outbound.create_balancer, {"tag": tag, "selector": ["relay"]})
    assert status == 400
    assert Balancer.query.count() == 0


def test_outbound_cannot_shadow_balancer(app):
    db.session.add(Balancer(tag="pool", selector='["relay"]'))
    db.session.commit()
    body, status = call(app, outbound.create_outbound, {"tag": "pool", "protocol": "freedom"})
    assert status == 400
    assert Outbound.query.filter_by(tag="pool").first() is None


@pytest.mark.parametrize("kind", ["outbound", "balancer"])
def test_referenced_routing_target_cannot_be_deleted(app, kind):
    if kind == "balancer":
        db.session.add(Balancer(tag="pool", selector='["relay"]'))
    tag = "pool" if kind == "balancer" else "relay"
    db.session.add(RoutingProfile(name="route", rules=json.dumps([{"domain": ["example.org"], "outboundTag": tag}])))
    db.session.commit()
    handler = outbound.delete_balancer if kind == "balancer" else outbound.delete_outbound
    body, status = call(app, handler, None, tag, method="DELETE")
    assert status == 400
    model = Balancer if kind == "balancer" else Outbound
    assert model.query.filter_by(tag=tag).first() is not None


@pytest.mark.parametrize("kind", ["reality", "wireguard"])
def test_builder_rejects_mismatched_keypair(kind):
    generate = protocol.generate_reality_keys if kind == "reality" else protocol.generate_wireguard_keys
    first, second = generate(), generate()
    if kind == "reality":
        data = {
            "protocol": "vless",
            "security": "reality",
            "realityPrivateKey": first["privateKey"],
            "realityPublicKey": second["publicKey"],
        }
    else:
        data = {"protocol": "wireguard", "wgSecretKey": first["privateKey"], "wgPublicKey": second["publicKey"]}
    with pytest.raises(ValueError, match="match"):
        protocol._build_stream_settings(data)


def test_tls_server_requires_certificate():
    with pytest.raises(ValueError, match="certificate"):
        protocol._build_stream_settings({"protocol": "vless", "security": "tls"})


def test_ss2022_chacha_is_rejected_for_panel_users():
    method = "2022-blake3-chacha20-poly1305"
    with pytest.raises(ValueError, match="multi-user"):
        protocol._build_stream_settings(
            {
                "protocol": "shadowsocks",
                "ssMethod": method,
                "ssPassword": protocol.generate_shadowsocks_password(method),
            }
        )


def test_bulk_reset_preserves_reenable_and_reports_partial_failure(app, monkeypatch):
    add_client(enable=False)

    def reset(tag, email, reenable=False):
        client = Client.query.filter_by(inbound_tag=tag, email=email).first()
        if client is None:
            raise ValueError("User not found")
        client.enable = reenable
        db.session.commit()

    monkeypatch.setattr(inbound, "reset_user_traffic", reset)
    body, status = call(
        app,
        inbound.reset_user_traffic_route,
        {"users": [{"tag": "ib", "email": "old", "reenable": True}, {"tag": "ib", "email": "missing"}]},
    )
    assert status == 200
    assert body["reset"] == 1
    assert body["failed_users"] == [{"tag": "ib", "email": "missing"}]
    assert Client.query.filter_by(email="old").one().enable is True


def test_wireguard_assignment_persists_when_other_clients_change(app):
    from panel_core.services.wireguard import ensure_wireguard_addresses

    ib = Inbound.query.filter_by(tag="ib").one()
    ib.protocol = "wireguard"
    first = Client(id=base64.b64encode(b"a" * 32).decode(), inbound_tag="ib", email="first", enable=True)
    db.session.add(first)
    db.session.commit()
    ensure_wireguard_addresses(ib)
    db.session.commit()
    original = first.wg_address
    assert original and original.endswith("/32")
    second = Client(id=base64.b64encode(b"b" * 32).decode(), inbound_tag="ib", email="second", enable=True)
    db.session.add(second)
    first.enable = False
    db.session.commit()
    ensure_wireguard_addresses(ib)
    db.session.commit()
    assert first.wg_address == original
    assert second.wg_address != original
    assert first.to_dict()["wg_address"] == original


def test_wireguard_rejects_duplicate_assigned_addresses(app):
    from panel_core.services.wireguard import ensure_wireguard_addresses

    ib = Inbound.query.filter_by(tag="ib").one()
    ib.protocol = "wireguard"
    for i, letter in enumerate([b"a", b"b"]):
        db.session.add(
            Client(
                id=base64.b64encode(letter * 32).decode(),
                inbound_tag="ib",
                email=str(i),
                enable=True,
                wg_address="172.19.0.4/32",
            )
        )
    db.session.commit()
    with pytest.raises(ValueError, match="duplicate|Duplicate"):
        ensure_wireguard_addresses(ib)


def test_unrelated_edit_repairs_legacy_public_key(app):
    first, second = protocol.generate_reality_keys(), protocol.generate_reality_keys()
    ib = Inbound.query.filter_by(tag="ib").one()
    ib.stream_settings = json.dumps(
        {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "privateKey": first["privateKey"],
                "publicKey": second["publicKey"],
                "serverNames": ["example.org"],
                "dest": "example.org:443",
            },
        }
    )
    db.session.commit()
    body, status = call(app, inbound.update_inbound, {"label": "updated"}, "ib")
    assert status == 200
    assert json.loads(ib.stream_settings)["realitySettings"]["publicKey"] == first["publicKey"]


def test_malformed_remote_reset_never_reports_success(app, monkeypatch):
    from panel_core.services import panel_proxy

    monkeypatch.setattr(panel_proxy, "proxy_bulk_reset_traffic", lambda *args: {"reset": 1, "failed_users": [{}]})
    body, status = call(
        app,
        inbound.reset_user_traffic_route,
        {"users": [{"tag": "ib", "email": "one", "panel_id": 1}, {"tag": "ib", "email": "two", "panel_id": 1}]},
    )
    assert status == 200
    assert body["reset"] == 0
    assert len(body["failed_users"]) == 2


def test_failed_apply_saved_user_is_recovered_without_creating_another(app, monkeypatch):
    from panel_core.models import RuntimeApplyState
    from panel_core.services import runtime_apply

    def generate(**kwargs):
        if kwargs.get("publish", True):
            raise RuntimeError("disk unavailable after commit")

    monkeypatch.setattr(runtime_apply, "generate_config_file", generate)
    body, status = call(app, inbound.add_user, {"email": "saved", "enable": False}, "ib")
    assert status == 503
    assert body["saved"] is True
    assert Client.query.filter_by(email="saved").count() == 1
    state = db.session.get(RuntimeApplyState, 1)
    assert state.desired_revision > state.applied_revision
    monkeypatch.setattr(runtime_apply, "generate_config_file", lambda **kwargs: None)
    assert runtime_apply.retry_pending_runtime()
    assert Client.query.filter_by(email="saved").count() == 1
    assert (
        db.session.get(RuntimeApplyState, 1).desired_revision == db.session.get(RuntimeApplyState, 1).applied_revision
    )


def test_repeating_saved_log_level_retries_pending_apply(app, monkeypatch):
    from panel_core.api import system
    from panel_core.models import RuntimeApplyState, SystemSetting
    from panel_core.services import runtime_apply

    monkeypatch.setattr(system, "has_local_xray", lambda: True)

    def fail():
        raise RuntimeError("node offline")

    monkeypatch.setattr(system, "restart_xray_container", fail)
    body, status = call(app, system.system_settings_update, {"xrayLogLevel": "debug"})
    assert status == 503
    assert db.session.get(SystemSetting, "xray_log_level").value == "debug"
    revision = db.session.get(RuntimeApplyState, 1).desired_revision
    monkeypatch.setattr(runtime_apply, "restart_xray_container", lambda: None)
    body, status = call(app, system.system_settings_update, {"xrayLogLevel": "debug"})
    assert status == 200
    assert db.session.get(RuntimeApplyState, 1).applied_revision == revision
