import json
import time
import uuid
from unittest.mock import patch

import jwt
import pytest

from panel_core.extensions import db
from panel_core.models import Admin, Inbound, Client, Tariff, TariffItem
from panel_core.utils import SECRET_KEY


@pytest.fixture
def app(app):

    from panel_core.api import inbound as inbound_api
    from panel_core.api import federation as federation_api

    if not any(bp.name == "inbound" for bp in app.blueprints.values()):
        app.register_blueprint(inbound_api.bp, url_prefix="/api")
    if not any(bp.name == "federation" for bp in app.blueprints.values()):
        app.register_blueprint(federation_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(app):
    pwd_version = int(time.time())
    admin = Admin(
        username="admin",
        password="hashed-not-checked-by-token-required",
        password_changed_at=pwd_version,
    )
    db.session.add(admin)
    db.session.commit()
    return admin


@pytest.fixture
def auth_headers(admin):

    token = jwt.encode(
        {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": int(admin.password_changed_at),
            "exp": int(time.time()) + 3600,
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


_COMMON_PATCHES = [
    "panel_core.api.inbound.generate_config_file",
    "panel_core.api.inbound.restart_xray_container",
    "panel_core.services.xray.generate_config_file",
    "panel_core.services.xray.restart_xray_container",
    "panel_core.services.stats._api_add_user_grpc",
    "panel_core.services.stats._api_remove_user_grpc",
    "panel_core.services.sub_cache.invalidate_user",
    "panel_core.services.sub_cache.invalidate_all_for_inbound",
]


@pytest.fixture(autouse=True)
def _mock_infra():

    patchers = []
    mocks = {}
    for target in _COMMON_PATCHES:
        p = patch(target)
        m = p.start()
        if target.endswith("_api_add_user_grpc"):
            m.return_value = True
        elif target.endswith("_api_remove_user_grpc"):
            m.return_value = True
        mocks[target] = m
        patchers.append(p)
    yield mocks
    for p in patchers:
        p.stop()


def _make_inbound(tag="vless-in", port=10443, protocol="vless", label=None):

    stream = json.dumps({"network": "tcp", "security": "none"})
    ib = Inbound(
        tag=tag,
        port=port,
        protocol=protocol,
        stream_settings=stream,
        label=label,
    )
    db.session.add(ib)
    db.session.commit()
    return ib


def _make_client(inbound_tag="vless-in", email="alice", client_id=None, **kwargs):

    if client_id is None:
        client_id = str(uuid.uuid4())
    c = Client(
        id=client_id,
        email=email,
        inbound_tag=inbound_tag,
        limit_bytes=kwargs.get("limit_bytes", 0),
        expiry_time=kwargs.get("expiry_time", 0),
        enable=kwargs.get("enable", True),
        reset_day=kwargs.get("reset_day", 0),
        flow=kwargs.get("flow", "xtls-rprx-vision"),
        telegram_id=kwargs.get("telegram_id"),
    )
    db.session.add(c)
    db.session.commit()
    return c


class TestGetInbounds:
    def test_empty_list(self, client, auth_headers):
        resp = client.get("/api/inbounds?panel=local", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_inbound_with_clients(self, app, client, auth_headers):
        ib = _make_inbound()
        _make_client(inbound_tag=ib.tag)
        resp = client.get("/api/inbounds?panel=local", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["tag"] == "vless-in"
        assert data[0]["port"] == 10443
        assert data[0]["protocol"] == "vless"
        assert data[0]["panel_id"] is None
        assert data[0]["panel_name"] == "Master"
        clients = data[0]["settings"]["clients"]
        assert len(clients) == 1
        assert clients[0]["email"] == "alice"

    def test_non_panel_user_protocol_returns_empty_clients(self, app, client, auth_headers):

        _make_inbound(tag="socks-in", port=1080, protocol="socks")
        resp = client.get("/api/inbounds?panel=local", headers=auth_headers)
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["settings"]["clients"] == []

    def test_label_included(self, app, client, auth_headers):
        _make_inbound(tag="labeled", port=9999, label="Germany VPN")
        resp = client.get("/api/inbounds?panel=local", headers=auth_headers)
        assert resp.get_json()[0]["label"] == "Germany VPN"

    def test_client_sub_url_exposed(self, app, client, auth_headers, monkeypatch):

        from panel_core.models import TelegramUser

        monkeypatch.setenv("PANEL_DOMAIN", "panel.example.com")
        monkeypatch.delenv("SUB_DOMAIN", raising=False)

        ib = _make_inbound()
        _make_client(inbound_tag=ib.tag, email="with-tg", telegram_id=555)
        _make_client(inbound_tag=ib.tag, email="no-tg")
        db.session.add(TelegramUser(telegram_id=555, sub_token="someToken"))
        db.session.commit()

        resp = client.get("/api/inbounds?panel=local", headers=auth_headers)
        assert resp.status_code == 200
        clients = resp.get_json()[0]["settings"]["clients"]
        by_email = {c["email"]: c for c in clients}
        assert by_email["with-tg"]["sub_url"].endswith("/api/sub/u/someToken")
        assert by_email["no-tg"]["sub_url"] is None


class TestCreateInbound:
    def test_create_vless_minimal(self, client, auth_headers):
        resp = client.post(
            "/api/inbounds",
            headers=auth_headers,
            json={
                "tag": "vless-new",
                "port": 20443,
                "protocol": "vless",
                "network": "tcp",
                "security": "none",
            },
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["tag"] == "vless-new"
        assert body["port"] == 20443

        ib = Inbound.query.filter_by(tag="vless-new").first()
        assert ib is not None
        assert ib.port == 20443
        assert ib.protocol == "vless"

    def test_create_duplicate_tag_returns_400(self, app, client, auth_headers):
        _make_inbound(tag="dup", port=11111)
        resp = client.post(
            "/api/inbounds",
            headers=auth_headers,
            json={"tag": "dup", "port": 22222, "protocol": "vless", "network": "tcp", "security": "none"},
        )
        assert resp.status_code == 400
        assert "exists" in resp.get_json()["error"].lower()

    def test_create_duplicate_port_returns_400(self, app, client, auth_headers):
        _make_inbound(tag="first", port=33333)
        resp = client.post(
            "/api/inbounds",
            headers=auth_headers,
            json={"tag": "second", "port": 33333, "protocol": "vless", "network": "tcp", "security": "none"},
        )
        assert resp.status_code == 400
        assert "exists" in resp.get_json()["error"].lower()

    def test_reserved_api_tag_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/inbounds",
            headers=auth_headers,
            json={"tag": "api", "port": 44444, "protocol": "vless", "network": "tcp", "security": "none"},
        )
        assert resp.status_code == 400
        assert "reserved" in resp.get_json()["error"].lower()

    def test_invalid_port_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/inbounds",
            headers=auth_headers,
            json={"tag": "bad-port", "port": 99999, "protocol": "vless", "network": "tcp", "security": "none"},
        )
        assert resp.status_code == 400

    def test_unsupported_protocol_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/inbounds",
            headers=auth_headers,
            json={"tag": "bad-proto", "port": 55555, "protocol": "quic-magic", "network": "tcp", "security": "none"},
        )
        assert resp.status_code == 400
        assert "unsupported" in resp.get_json()["error"].lower()

    def test_create_with_label(self, client, auth_headers):
        resp = client.post(
            "/api/inbounds",
            headers=auth_headers,
            json={
                "tag": "labeled-ib",
                "port": 12345,
                "protocol": "vless",
                "network": "tcp",
                "security": "none",
                "label": "My Label",
            },
        )
        assert resp.status_code == 201
        assert Inbound.query.filter_by(tag="labeled-ib").first().label == "My Label"


class TestUpdateInbound:
    def test_update_port_and_label(self, app, client, auth_headers):
        _make_inbound(tag="upd", port=8001)
        resp = client.put(
            "/api/inbounds/upd",
            headers=auth_headers,
            json={"port": 8002, "label": "Updated"},
        )
        assert resp.status_code == 200
        ib = Inbound.query.filter_by(tag="upd").first()
        assert ib.port == 8002
        assert ib.label == "Updated"

    def test_update_not_found(self, client, auth_headers):
        resp = client.put("/api/inbounds/nope", headers=auth_headers, json={"port": 9999})
        assert resp.status_code == 404

    def test_update_duplicate_port_returns_400(self, app, client, auth_headers):
        _make_inbound(tag="a", port=7001)
        _make_inbound(tag="b", port=7002)
        resp = client.put(
            "/api/inbounds/b",
            headers=auth_headers,
            json={"port": 7001},
        )
        assert resp.status_code == 400
        assert "exists" in resp.get_json()["error"].lower()

    def test_update_device_limit(self, app, client, auth_headers):
        _make_inbound(tag="dl", port=6001)
        resp = client.put(
            "/api/inbounds/dl",
            headers=auth_headers,
            json={"device_limit": 5},
        )
        assert resp.status_code == 200
        assert Inbound.query.filter_by(tag="dl").first().device_limit == 5

    def test_switching_transport_to_xhttp_clears_client_flow(self, app, client, auth_headers):

        ib = Inbound(
            tag="flow-xhttp",
            port=8101,
            protocol="vless",
            stream_settings=json.dumps({"network": "tcp", "security": "tls"}),
        )
        db.session.add(ib)
        db.session.commit()
        _make_client(inbound_tag="flow-xhttp", email="u1", flow="xtls-rprx-vision")

        resp = client.put("/api/inbounds/flow-xhttp", headers=auth_headers, json={"network": "xhttp"})
        assert resp.status_code == 200
        assert Client.query.filter_by(inbound_tag="flow-xhttp", email="u1").first().flow == ""

    def test_unrelated_update_keeps_compatible_flow(self, app, client, auth_headers):

        ib = Inbound(
            tag="flow-keep",
            port=8102,
            protocol="vless",
            stream_settings=json.dumps({"network": "tcp", "security": "tls"}),
        )
        db.session.add(ib)
        db.session.commit()
        _make_client(inbound_tag="flow-keep", email="u1", flow="xtls-rprx-vision")

        resp = client.put("/api/inbounds/flow-keep", headers=auth_headers, json={"label": "x"})
        assert resp.status_code == 200
        assert Client.query.filter_by(inbound_tag="flow-keep", email="u1").first().flow == "xtls-rprx-vision"

    def test_switching_to_non_panel_protocol_removes_clients(self, app, client, auth_headers):

        _make_inbound(tag="switch", port=6100, protocol="vless")
        _make_client(inbound_tag="switch", email="user1")
        assert Client.query.filter_by(inbound_tag="switch").count() == 1
        resp = client.put(
            "/api/inbounds/switch",
            headers=auth_headers,
            json={"protocol": "socks"},
        )
        assert resp.status_code == 200
        assert Client.query.filter_by(inbound_tag="switch").count() == 0


class TestDeleteInbound:
    def test_delete_existing(self, app, client, auth_headers):
        _make_inbound(tag="del-me", port=5001)
        _make_client(inbound_tag="del-me")
        resp = client.delete("/api/inbounds/del-me", headers=auth_headers)
        assert resp.status_code == 200
        assert Inbound.query.filter_by(tag="del-me").first() is None
        assert Client.query.filter_by(inbound_tag="del-me").count() == 0

    def test_delete_not_found(self, client, auth_headers):
        resp = client.delete("/api/inbounds/ghost", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_local_inbound_purges_local_tariff_items_only(self, app, client, auth_headers):
        _make_inbound(tag="okins", port=5002)
        tariff = Tariff(name="Mix", price_rub=100, period_days=30)
        db.session.add(tariff)
        db.session.commit()
        local_item = TariffItem(tariff_id=tariff.id, inbound_tag="okins", traffic_gb=0, panel_id=None, sort_order=0)
        remote_same_tag = TariffItem(tariff_id=tariff.id, inbound_tag="okins", traffic_gb=0, panel_id=5, sort_order=1)
        db.session.add_all([local_item, remote_same_tag])
        db.session.commit()
        local_id, remote_id = local_item.id, remote_same_tag.id

        resp = client.delete("/api/inbounds/okins", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["removed_tariff_items"] == 1
        assert TariffItem.query.filter_by(id=local_id).count() == 0
        assert TariffItem.query.filter_by(id=remote_id).count() == 1

    @patch("panel_core.services.panel_proxy.proxy_delete_inbound")
    def test_delete_remote_inbound_purges_panel_items(self, mock_proxy, app, client, auth_headers):
        mock_proxy.return_value = {"status": "deleted"}
        tariff = Tariff(name="Rem", price_rub=100, period_days=30)
        db.session.add(tariff)
        db.session.commit()
        remote_item = TariffItem(tariff_id=tariff.id, inbound_tag="hiks", traffic_gb=0, panel_id=5, sort_order=0)
        other_panel = TariffItem(tariff_id=tariff.id, inbound_tag="hiks", traffic_gb=0, panel_id=9, sort_order=1)
        db.session.add_all([remote_item, other_panel])
        db.session.commit()
        remote_id, other_id = remote_item.id, other_panel.id

        resp = client.delete("/api/inbounds/hiks?panel_id=5", headers=auth_headers)
        assert resp.status_code == 200
        mock_proxy.assert_called_once_with(5, "hiks")
        assert TariffItem.query.filter_by(id=remote_id).count() == 0
        assert TariffItem.query.filter_by(id=other_id).count() == 1


class TestAddUser:
    def test_add_user_auto_uuid(self, app, client, auth_headers):
        _make_inbound(tag="ib-users", port=4001)
        resp = client.post(
            "/api/inbounds/ib-users/users",
            headers=auth_headers,
            json={"email": "bob"},
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["email"] == "bob"

        uuid.UUID(body["id"])
        assert body["inbound_tag"] == "ib-users"
        assert body["enable"] is True

        assert Client.query.filter_by(inbound_tag="ib-users", email="bob").first() is not None

    def test_add_user_with_explicit_uuid(self, app, client, auth_headers):
        _make_inbound(tag="ib-explicit", port=4002)
        uid = str(uuid.uuid4())
        resp = client.post(
            "/api/inbounds/ib-explicit/users",
            headers=auth_headers,
            json={"email": "charlie", "id": uid},
        )
        assert resp.status_code == 201
        assert resp.get_json()["id"] == uid

    def test_add_user_duplicate_email_returns_400(self, app, client, auth_headers):
        _make_inbound(tag="ib-dup", port=4003)
        _make_client(inbound_tag="ib-dup", email="dup-email")
        resp = client.post(
            "/api/inbounds/ib-dup/users",
            headers=auth_headers,
            json={"email": "dup-email"},
        )
        assert resp.status_code == 400
        assert "exists" in resp.get_json()["error"].lower()

    def test_add_user_nonexistent_inbound_returns_404(self, client, auth_headers):
        resp = client.post(
            "/api/inbounds/ghost/users",
            headers=auth_headers,
            json={"email": "nobody"},
        )
        assert resp.status_code == 404

    def test_add_user_to_socks_inbound_returns_400(self, app, client, auth_headers):
        _make_inbound(tag="socks-ib", port=4004, protocol="socks")
        resp = client.post(
            "/api/inbounds/socks-ib/users",
            headers=auth_headers,
            json={"email": "socks-user"},
        )
        assert resp.status_code == 400
        assert "does not support panel users" in resp.get_json()["error"]

    def test_add_user_with_limits(self, app, client, auth_headers):
        _make_inbound(tag="ib-limits", port=4005)
        resp = client.post(
            "/api/inbounds/ib-limits/users",
            headers=auth_headers,
            json={
                "email": "limited",
                "limit_bytes": 1073741824,
                "expiry_time": 9999999999000,
                "reset_day": 15,
                "enable": False,
            },
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["limit_bytes"] == 1073741824
        assert body["expiry_time"] == 9999999999000
        assert body["reset_day"] == 15
        assert body["enable"] is False

    def test_add_user_invalid_uuid_for_vless_returns_400(self, app, client, auth_headers):
        _make_inbound(tag="ib-baduuid", port=4006)
        resp = client.post(
            "/api/inbounds/ib-baduuid/users",
            headers=auth_headers,
            json={"email": "bad", "id": "not-a-uuid"},
        )
        assert resp.status_code == 400
        assert "UUID" in resp.get_json()["error"]


class TestVlessFlowTransportCompat:
    def _make_vless_inbound(self, tag, port, network, security):
        ib = Inbound(
            tag=tag,
            port=port,
            protocol="vless",
            stream_settings=json.dumps({"network": network, "security": security}),
        )
        db.session.add(ib)
        db.session.commit()
        return ib

    def test_add_user_default_flow_empty_on_xhttp(self, app, client, auth_headers):
        self._make_vless_inbound("fc-xh", 8201, "xhttp", "tls")
        resp = client.post("/api/inbounds/fc-xh/users", headers=auth_headers, json={"email": "u1"})
        assert resp.status_code == 201
        assert resp.get_json()["flow"] == ""

    def test_add_user_default_flow_vision_on_tcp_reality(self, app, client, auth_headers):
        self._make_vless_inbound("fc-tcp", 8202, "tcp", "reality")
        resp = client.post("/api/inbounds/fc-tcp/users", headers=auth_headers, json={"email": "u1"})
        assert resp.status_code == 201
        assert resp.get_json()["flow"] == "xtls-rprx-vision"

    def test_add_user_explicit_vision_on_xhttp_is_cleared(self, app, client, auth_headers):
        self._make_vless_inbound("fc-xh2", 8203, "xhttp", "tls")
        resp = client.post(
            "/api/inbounds/fc-xh2/users",
            headers=auth_headers,
            json={"email": "u1", "flow": "xtls-rprx-vision"},
        )
        assert resp.status_code == 201
        assert resp.get_json()["flow"] == ""
        assert Client.query.filter_by(inbound_tag="fc-xh2", email="u1").first().flow == ""

    def test_update_user_vision_on_ws_is_cleared(self, app, client, auth_headers):
        self._make_vless_inbound("fc-ws", 8204, "ws", "tls")
        _make_client(inbound_tag="fc-ws", email="u1", flow="")
        resp = client.put(
            "/api/inbounds/fc-ws/users",
            headers=auth_headers,
            json={"old_email": "u1", "flow": "xtls-rprx-vision"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["flow"] == ""
        assert Client.query.filter_by(inbound_tag="fc-ws", email="u1").first().flow == ""

    def test_update_user_vision_on_tcp_tls_is_kept(self, app, client, auth_headers):
        self._make_vless_inbound("fc-tls", 8205, "tcp", "tls")
        _make_client(inbound_tag="fc-tls", email="u1", flow="")
        resp = client.put(
            "/api/inbounds/fc-tls/users",
            headers=auth_headers,
            json={"old_email": "u1", "flow": "xtls-rprx-vision"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["flow"] == "xtls-rprx-vision"

    def test_update_user_legacy_vision_on_xhttp_normalized_on_save(self, app, client, auth_headers):
        self._make_vless_inbound("fc-legacy", 8206, "xhttp", "tls")
        _make_client(inbound_tag="fc-legacy", email="u1", flow="xtls-rprx-vision")
        resp = client.put(
            "/api/inbounds/fc-legacy/users",
            headers=auth_headers,
            json={"old_email": "u1", "limit_bytes": 1024},
        )
        assert resp.status_code == 200
        assert resp.get_json()["flow"] == ""


class TestUpdateUser:
    def test_update_email_and_limit(self, app, client, auth_headers):
        _make_inbound(tag="ib-upd", port=3001)
        _make_client(inbound_tag="ib-upd", email="old-name")
        resp = client.put(
            "/api/inbounds/ib-upd/users",
            headers=auth_headers,
            json={
                "old_email": "old-name",
                "new_email": "new-name",
                "limit_bytes": 5368709120,
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["email"] == "new-name"
        assert body["limit_bytes"] == 5368709120

        assert Client.query.filter_by(inbound_tag="ib-upd", email="old-name").first() is None
        assert Client.query.filter_by(inbound_tag="ib-upd", email="new-name").first() is not None

    def test_update_user_not_found(self, app, client, auth_headers):
        _make_inbound(tag="ib-nf", port=3002)
        resp = client.put(
            "/api/inbounds/ib-nf/users",
            headers=auth_headers,
            json={"old_email": "ghost", "new_email": "ghost2"},
        )
        assert resp.status_code == 404

    def test_update_duplicate_email_returns_400(self, app, client, auth_headers):
        _make_inbound(tag="ib-demail", port=3003)
        _make_client(inbound_tag="ib-demail", email="user-a")
        _make_client(inbound_tag="ib-demail", email="user-b")
        resp = client.put(
            "/api/inbounds/ib-demail/users",
            headers=auth_headers,
            json={"old_email": "user-a", "new_email": "user-b"},
        )
        assert resp.status_code == 400
        assert "exists" in resp.get_json()["error"].lower()

    def test_update_enable_toggle(self, app, client, auth_headers):
        _make_inbound(tag="ib-toggle", port=3004)
        _make_client(inbound_tag="ib-toggle", email="toggler", enable=True)
        resp = client.put(
            "/api/inbounds/ib-toggle/users",
            headers=auth_headers,
            json={"old_email": "toggler", "enable": False},
        )
        assert resp.status_code == 200
        assert resp.get_json()["enable"] is False

    def test_update_inbound_not_found(self, client, auth_headers):
        resp = client.put(
            "/api/inbounds/ghost-ib/users",
            headers=auth_headers,
            json={"old_email": "x"},
        )
        assert resp.status_code == 404

    def test_update_socks_inbound_returns_400(self, app, client, auth_headers):
        _make_inbound(tag="socks-upd", port=3005, protocol="socks")
        resp = client.put(
            "/api/inbounds/socks-upd/users",
            headers=auth_headers,
            json={"old_email": "x"},
        )
        assert resp.status_code == 400
        assert "does not support panel users" in resp.get_json()["error"]


class TestDeleteUser:
    def test_delete_user(self, app, client, auth_headers):
        _make_inbound(tag="ib-del", port=2001)
        _make_client(inbound_tag="ib-del", email="doomed")
        resp = client.delete(
            "/api/inbounds/ib-del/users?email=doomed",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert Client.query.filter_by(inbound_tag="ib-del", email="doomed").first() is None

    def test_delete_user_not_found(self, app, client, auth_headers):
        _make_inbound(tag="ib-del2", port=2002)
        resp = client.delete(
            "/api/inbounds/ib-del2/users?email=ghost",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_delete_user_on_socks_inbound_returns_400(self, app, client, auth_headers):
        _make_inbound(tag="socks-del", port=2003, protocol="socks")
        resp = client.delete(
            "/api/inbounds/socks-del/users?email=x",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "does not support panel users" in resp.get_json()["error"]


class TestResetTraffic:
    @patch("panel_core.api.inbound.reset_user_traffic")
    def test_single_reset(self, mock_reset, app, client, auth_headers):
        _make_inbound(tag="rst-ib", port=1501)
        _make_client(inbound_tag="rst-ib", email="rst-user")
        resp = client.post(
            "/api/users/reset-traffic",
            headers=auth_headers,
            json={"tag": "rst-ib", "email": "rst-user"},
        )
        assert resp.status_code == 200
        mock_reset.assert_called_once_with("rst-ib", "rst-user")

    @patch("panel_core.api.inbound.reset_user_traffic")
    def test_bulk_reset(self, mock_reset, app, client, auth_headers):
        _make_inbound(tag="brst-ib", port=1502)
        _make_client(inbound_tag="brst-ib", email="u1")
        _make_client(inbound_tag="brst-ib", email="u2")
        resp = client.post(
            "/api/users/reset-traffic",
            headers=auth_headers,
            json={
                "users": [
                    {"tag": "brst-ib", "email": "u1"},
                    {"tag": "brst-ib", "email": "u2"},
                ]
            },
        )
        assert resp.status_code == 200
        assert mock_reset.call_count == 2


class TestBulkEnable:
    def test_bulk_disable(self, app, client, auth_headers):
        _make_inbound(tag="be-ib", port=1601)
        _make_client(inbound_tag="be-ib", email="en1", enable=True)
        _make_client(inbound_tag="be-ib", email="en2", enable=True)
        resp = client.post(
            "/api/users/bulk-enable",
            headers=auth_headers,
            json={
                "users": [
                    {"tag": "be-ib", "email": "en1"},
                    {"tag": "be-ib", "email": "en2"},
                ],
                "enable": False,
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["count"] == 2
        assert Client.query.filter_by(inbound_tag="be-ib", email="en1").first().enable is False
        assert Client.query.filter_by(inbound_tag="be-ib", email="en2").first().enable is False

    def test_bulk_enable(self, app, client, auth_headers):
        _make_inbound(tag="be-ib2", port=1602)
        _make_client(inbound_tag="be-ib2", email="dis1", enable=False)
        resp = client.post(
            "/api/users/bulk-enable",
            headers=auth_headers,
            json={
                "users": [{"tag": "be-ib2", "email": "dis1"}],
                "enable": True,
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 1
        assert Client.query.filter_by(inbound_tag="be-ib2", email="dis1").first().enable is True

    def test_bulk_enable_missing_enable_field(self, client, auth_headers):
        resp = client.post(
            "/api/users/bulk-enable",
            headers=auth_headers,
            json={"users": [{"tag": "x", "email": "y"}]},
        )
        assert resp.status_code == 400
        assert "enable" in resp.get_json()["error"].lower()

    def test_bulk_enable_missing_users(self, client, auth_headers):
        resp = client.post(
            "/api/users/bulk-enable",
            headers=auth_headers,
            json={"enable": True},
        )
        assert resp.status_code == 400

    def test_bulk_enable_noop_when_already_in_state(self, app, client, auth_headers):
        _make_inbound(tag="be-ib3", port=1603)
        _make_client(inbound_tag="be-ib3", email="noop", enable=True)
        resp = client.post(
            "/api/users/bulk-enable",
            headers=auth_headers,
            json={
                "users": [{"tag": "be-ib3", "email": "noop"}],
                "enable": True,
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 0


class TestAuth:
    def test_get_inbounds_no_token(self, client):
        resp = client.get("/api/inbounds")
        assert resp.status_code == 401

    def test_create_inbound_no_token(self, client):
        resp = client.post("/api/inbounds", json={"tag": "x", "port": 1})
        assert resp.status_code == 401

    def test_update_inbound_no_token(self, client):
        resp = client.put("/api/inbounds/x", json={})
        assert resp.status_code == 401

    def test_delete_inbound_no_token(self, client):
        resp = client.delete("/api/inbounds/x")
        assert resp.status_code == 401

    def test_add_user_no_token(self, client):
        resp = client.post("/api/inbounds/x/users", json={"email": "y"})
        assert resp.status_code == 401

    def test_update_user_no_token(self, client):
        resp = client.put("/api/inbounds/x/users", json={})
        assert resp.status_code == 401

    def test_delete_user_no_token(self, client):
        resp = client.delete("/api/inbounds/x/users?email=y")
        assert resp.status_code == 401

    def test_reset_traffic_no_token(self, client):
        resp = client.post("/api/users/reset-traffic", json={"tag": "x", "email": "y"})
        assert resp.status_code == 401

    def test_bulk_enable_no_token(self, client):
        resp = client.post("/api/users/bulk-enable", json={"users": [], "enable": True})
        assert resp.status_code == 401


class TestBulkDelete:
    @patch("panel_core.api.inbound.bulk_delete_users", return_value=2)
    def test_bulk_delete(self, mock_bd, app, client, auth_headers):
        _make_inbound(tag="bd-ib", port=1701)
        _make_client(inbound_tag="bd-ib", email="d1")
        _make_client(inbound_tag="bd-ib", email="d2")
        resp = client.post(
            "/api/users/bulk-delete",
            headers=auth_headers,
            json={
                "users": [
                    {"tag": "bd-ib", "email": "d1"},
                    {"tag": "bd-ib", "email": "d2"},
                ]
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 2

    def test_bulk_delete_missing_users(self, client, auth_headers):
        resp = client.post("/api/users/bulk-delete", headers=auth_headers, json={})
        assert resp.status_code == 400


class TestBulkAdjustDays:
    def test_add_days(self, app, client, auth_headers):
        _make_inbound(tag="adj-ib", port=1801)
        now_ms = int(time.time() * 1000)
        _make_client(inbound_tag="adj-ib", email="adj1", expiry_time=now_ms)
        resp = client.post(
            "/api/users/bulk-adjust-days",
            headers=auth_headers,
            json={
                "users": [{"tag": "adj-ib", "email": "adj1"}],
                "days": 7,
                "mode": "add",
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["updated"] == 1
        c = Client.query.filter_by(inbound_tag="adj-ib", email="adj1").first()

        assert c.expiry_time >= now_ms + 7 * 86_400_000

    def test_skip_no_expiry(self, app, client, auth_headers):

        _make_inbound(tag="adj-ib2", port=1802)
        _make_client(inbound_tag="adj-ib2", email="unlimited", expiry_time=0)
        resp = client.post(
            "/api/users/bulk-adjust-days",
            headers=auth_headers,
            json={
                "users": [{"tag": "adj-ib2", "email": "unlimited"}],
                "days": 7,
                "mode": "add",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["skipped"] == 1


class TestBulkAdjustTraffic:
    def test_add_traffic(self, app, client, auth_headers):
        _make_inbound(tag="traf-ib", port=1901)
        _make_client(inbound_tag="traf-ib", email="traf1", limit_bytes=1024**3)
        resp = client.post(
            "/api/users/bulk-adjust-traffic",
            headers=auth_headers,
            json={
                "users": [{"tag": "traf-ib", "email": "traf1"}],
                "gb": 5,
                "mode": "add",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["updated"] == 1
        c = Client.query.filter_by(inbound_tag="traf-ib", email="traf1").first()
        assert c.limit_bytes == 1024**3 + 5 * 1024**3

    def test_skip_unlimited(self, app, client, auth_headers):

        _make_inbound(tag="traf-ib2", port=1902)
        _make_client(inbound_tag="traf-ib2", email="unlim", limit_bytes=0)
        resp = client.post(
            "/api/users/bulk-adjust-traffic",
            headers=auth_headers,
            json={
                "users": [{"tag": "traf-ib2", "email": "unlim"}],
                "gb": 1,
                "mode": "add",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["skipped"] == 1

    def test_subtract_below_zero_skipped(self, app, client, auth_headers):
        _make_inbound(tag="traf-ib3", port=1903)
        _make_client(inbound_tag="traf-ib3", email="tiny", limit_bytes=500_000_000)
        resp = client.post(
            "/api/users/bulk-adjust-traffic",
            headers=auth_headers,
            json={
                "users": [{"tag": "traf-ib3", "email": "tiny"}],
                "gb": 1,
                "mode": "subtract",
            },
        )
        assert resp.status_code == 200

        assert resp.get_json()["skipped"] == 1


class TestResetInboundTraffic:
    @patch("panel_core.api.inbound.reset_inbound_traffic")
    def test_reset_inbound_traffic(self, mock_reset, app, client, auth_headers):
        _make_inbound(tag="rit-ib", port=1401)
        resp = client.post("/api/inbounds/rit-ib/reset-traffic", headers=auth_headers)
        assert resp.status_code == 200
        mock_reset.assert_called_once_with("rit-ib")

    def test_reset_inbound_traffic_no_token(self, client):
        resp = client.post("/api/inbounds/x/reset-traffic")
        assert resp.status_code == 401


class TestBulkCrossPanelRouting:
    def test_bulk_delete_routes_remote_group_and_keeps_local(self, app, client, auth_headers):
        _make_inbound(tag="xp-local", port=2101)
        _make_client(inbound_tag="xp-local", email="loc1")

        with (
            patch("panel_core.api.inbound.bulk_delete_users", return_value=1) as mock_local,
            patch(
                "panel_core.services.panel_proxy.proxy_bulk_delete_users",
                return_value={"status": "deleted", "count": 2},
            ) as mock_remote,
        ):
            resp = client.post(
                "/api/users/bulk-delete",
                headers=auth_headers,
                json={
                    "users": [
                        {"tag": "xp-local", "email": "loc1"},
                        {"tag": "rem-in", "email": "r1", "panel_id": 7},
                        {"tag": "rem-in", "email": "r2", "panel_id": 7},
                    ]
                },
            )

        assert resp.status_code == 200

        assert resp.get_json()["count"] == 3

        mock_local.assert_called_once_with([{"tag": "xp-local", "email": "loc1"}])

        mock_remote.assert_called_once_with(7, [{"tag": "rem-in", "email": "r1"}, {"tag": "rem-in", "email": "r2"}])

    def test_bulk_delete_remote_failure_is_best_effort(self, app, client, auth_headers):
        _make_inbound(tag="xp-local2", port=2102)
        _make_client(inbound_tag="xp-local2", email="keep")

        with (
            patch("panel_core.api.inbound.bulk_delete_users", return_value=1),
            patch(
                "panel_core.services.panel_proxy.proxy_bulk_delete_users",
                side_effect=ValueError("Panel 'child' is offline"),
            ),
        ):
            resp = client.post(
                "/api/users/bulk-delete",
                headers=auth_headers,
                json={
                    "users": [
                        {"tag": "xp-local2", "email": "keep"},
                        {"tag": "rem-in", "email": "r1", "panel_id": 9},
                    ]
                },
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["count"] == 1
        assert "errors" in body and any("offline" in e for e in body["errors"])

    def test_bulk_enable_routes_remote_group(self, app, client, auth_headers):
        _make_inbound(tag="xp-en", port=2103)
        _make_client(inbound_tag="xp-en", email="e1", enable=False)

        with patch(
            "panel_core.services.panel_proxy.proxy_bulk_enable_users",
            return_value={"status": "ok", "count": 3},
        ) as mock_remote:
            resp = client.post(
                "/api/users/bulk-enable",
                headers=auth_headers,
                json={
                    "users": [
                        {"tag": "xp-en", "email": "e1"},
                        {"tag": "rem-in", "email": "r1", "panel_id": 4},
                    ],
                    "enable": True,
                },
            )

        assert resp.status_code == 200
        assert resp.get_json()["count"] == 4
        mock_remote.assert_called_once_with(4, [{"tag": "rem-in", "email": "r1"}], True)
        assert Client.query.filter_by(inbound_tag="xp-en", email="e1").first().enable is True

    def test_bulk_adjust_days_routes_remote_group(self, app, client, auth_headers):
        _make_inbound(tag="xp-adj", port=2104)
        now_ms = int(time.time() * 1000)
        _make_client(inbound_tag="xp-adj", email="a1", expiry_time=now_ms)

        with patch(
            "panel_core.services.panel_proxy.proxy_bulk_adjust_days",
            return_value={"status": "ok", "updated": 2, "skipped": 1},
        ) as mock_remote:
            resp = client.post(
                "/api/users/bulk-adjust-days",
                headers=auth_headers,
                json={
                    "users": [
                        {"tag": "xp-adj", "email": "a1"},
                        {"tag": "rem-in", "email": "r1", "panel_id": 5},
                    ],
                    "days": 7,
                    "mode": "add",
                },
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["updated"] == 3
        assert body["skipped"] == 1
        mock_remote.assert_called_once_with(5, [{"tag": "rem-in", "email": "r1"}], 7, "add")

    def test_bulk_adjust_traffic_routes_remote_group(self, app, client, auth_headers):
        _make_inbound(tag="xp-tr", port=2105)
        _make_client(inbound_tag="xp-tr", email="t1", limit_bytes=1024**3)

        with patch(
            "panel_core.services.panel_proxy.proxy_bulk_adjust_traffic",
            return_value={"status": "ok", "updated": 1, "skipped": 0},
        ) as mock_remote:
            resp = client.post(
                "/api/users/bulk-adjust-traffic",
                headers=auth_headers,
                json={
                    "users": [
                        {"tag": "xp-tr", "email": "t1"},
                        {"tag": "rem-in", "email": "r1", "panel_id": 6},
                    ],
                    "gb": 5,
                    "mode": "add",
                },
            )

        assert resp.status_code == 200
        assert resp.get_json()["updated"] == 2
        mock_remote.assert_called_once_with(6, [{"tag": "rem-in", "email": "r1"}], 5, "add")

    @patch("panel_core.api.inbound.reset_user_traffic")
    def test_bulk_reset_routes_remote_group(self, mock_local, app, client, auth_headers):
        _make_inbound(tag="xp-rst", port=2106)
        _make_client(inbound_tag="xp-rst", email="rs1")

        with patch(
            "panel_core.services.panel_proxy.proxy_bulk_reset_traffic",
            return_value={"status": "reset"},
        ) as mock_remote:
            resp = client.post(
                "/api/users/reset-traffic",
                headers=auth_headers,
                json={
                    "users": [
                        {"tag": "xp-rst", "email": "rs1"},
                        {"tag": "rem-in", "email": "r1", "panel_id": 3},
                    ]
                },
            )

        assert resp.status_code == 200
        mock_local.assert_called_once_with("xp-rst", "rs1")
        mock_remote.assert_called_once_with(3, [{"tag": "rem-in", "email": "r1"}])

    @patch("panel_core.api.inbound.reset_user_traffic")
    def test_single_reset_with_panel_id_proxies(self, mock_local, app, client, auth_headers):
        with patch(
            "panel_core.services.panel_proxy.proxy_bulk_reset_traffic",
            return_value={"status": "reset"},
        ) as mock_remote:
            resp = client.post(
                "/api/users/reset-traffic?panel_id=8",
                headers=auth_headers,
                json={"tag": "rem-in", "email": "solo"},
            )

        assert resp.status_code == 200
        mock_local.assert_not_called()
        mock_remote.assert_called_once_with(8, [{"tag": "rem-in", "email": "solo"}])


class TestBulkSetFlow:
    def test_enable_flow_on_vless(self, app, client, auth_headers):
        ib = Inbound(
            tag="fl-en",
            port=2201,
            protocol="vless",
            stream_settings=json.dumps({"network": "tcp", "security": "tls"}),
        )
        db.session.add(ib)
        db.session.commit()
        _make_client(inbound_tag="fl-en", email="f1", flow="")
        resp = client.post(
            "/api/users/bulk-set-flow",
            headers=auth_headers,
            json={"users": [{"tag": "fl-en", "email": "f1"}], "flow": "xtls-rprx-vision"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["updated"] == 1
        assert Client.query.filter_by(inbound_tag="fl-en", email="f1").first().flow == "xtls-rprx-vision"

    def test_disable_flow_on_vless(self, app, client, auth_headers):
        _make_inbound(tag="fl-dis", port=2202, protocol="vless")
        _make_client(inbound_tag="fl-dis", email="f1", flow="xtls-rprx-vision")
        resp = client.post(
            "/api/users/bulk-set-flow",
            headers=auth_headers,
            json={"users": [{"tag": "fl-dis", "email": "f1"}], "flow": ""},
        )
        assert resp.status_code == 200
        assert resp.get_json()["updated"] == 1
        assert Client.query.filter_by(inbound_tag="fl-dis", email="f1").first().flow == ""

    def test_skip_non_vless(self, app, client, auth_headers):
        _make_inbound(tag="fl-tr", port=2203, protocol="trojan")
        _make_client(inbound_tag="fl-tr", email="f1", flow="")
        resp = client.post(
            "/api/users/bulk-set-flow",
            headers=auth_headers,
            json={"users": [{"tag": "fl-tr", "email": "f1"}], "flow": "xtls-rprx-vision"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["updated"] == 0
        assert body["skipped"] == 1

    def test_skip_noop_when_already_on_target(self, app, client, auth_headers):
        _make_inbound(tag="fl-noop", port=2204, protocol="vless")
        _make_client(inbound_tag="fl-noop", email="f1", flow="xtls-rprx-vision")
        resp = client.post(
            "/api/users/bulk-set-flow",
            headers=auth_headers,
            json={"users": [{"tag": "fl-noop", "email": "f1"}], "flow": "xtls-rprx-vision"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["skipped"] == 1

    def test_invalid_flow_returns_400(self, app, client, auth_headers):
        _make_inbound(tag="fl-bad", port=2205, protocol="vless")
        _make_client(inbound_tag="fl-bad", email="f1")
        resp = client.post(
            "/api/users/bulk-set-flow",
            headers=auth_headers,
            json={"users": [{"tag": "fl-bad", "email": "f1"}], "flow": "bogus-flow"},
        )
        assert resp.status_code == 400

    def test_missing_flow_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/users/bulk-set-flow",
            headers=auth_headers,
            json={"users": [{"tag": "x", "email": "y"}]},
        )
        assert resp.status_code == 400
        assert "flow" in resp.get_json()["error"].lower()

    def test_no_token(self, client):
        resp = client.post("/api/users/bulk-set-flow", json={"users": [], "flow": ""})
        assert resp.status_code == 401

    def test_routes_remote_group(self, app, client, auth_headers):
        ib = Inbound(
            tag="fl-loc",
            port=2206,
            protocol="vless",
            stream_settings=json.dumps({"network": "tcp", "security": "tls"}),
        )
        db.session.add(ib)
        db.session.commit()
        _make_client(inbound_tag="fl-loc", email="f1", flow="")

        with patch(
            "panel_core.services.panel_proxy.proxy_bulk_set_flow",
            return_value={"status": "ok", "updated": 2, "skipped": 1},
        ) as mock_remote:
            resp = client.post(
                "/api/users/bulk-set-flow",
                headers=auth_headers,
                json={
                    "users": [
                        {"tag": "fl-loc", "email": "f1"},
                        {"tag": "rem-in", "email": "r1", "panel_id": 5},
                    ],
                    "flow": "xtls-rprx-vision",
                },
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["updated"] == 3
        assert body["skipped"] == 1
        mock_remote.assert_called_once_with(5, [{"tag": "rem-in", "email": "r1"}], "xtls-rprx-vision")

    def test_enable_flow_skips_incompatible_transport(self, app, client, auth_headers):

        ib = Inbound(
            tag="fl-xhttp",
            port=2210,
            protocol="vless",
            stream_settings=json.dumps({"network": "xhttp", "security": "tls"}),
        )
        db.session.add(ib)
        db.session.commit()
        _make_client(inbound_tag="fl-xhttp", email="f1", flow="")

        resp = client.post(
            "/api/users/bulk-set-flow",
            headers=auth_headers,
            json={"users": [{"tag": "fl-xhttp", "email": "f1"}], "flow": "xtls-rprx-vision"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["updated"] == 0
        assert body["skipped"] == 1
        assert Client.query.filter_by(inbound_tag="fl-xhttp", email="f1").first().flow == ""

    def test_disable_flow_allowed_on_incompatible_transport(self, app, client, auth_headers):

        ib = Inbound(
            tag="fl-xhttp2",
            port=2211,
            protocol="vless",
            stream_settings=json.dumps({"network": "xhttp", "security": "tls"}),
        )
        db.session.add(ib)
        db.session.commit()
        _make_client(inbound_tag="fl-xhttp2", email="f1", flow="xtls-rprx-vision")

        resp = client.post(
            "/api/users/bulk-set-flow",
            headers=auth_headers,
            json={"users": [{"tag": "fl-xhttp2", "email": "f1"}], "flow": ""},
        )
        assert resp.status_code == 200
        assert resp.get_json()["updated"] == 1
        assert Client.query.filter_by(inbound_tag="fl-xhttp2", email="f1").first().flow == ""
