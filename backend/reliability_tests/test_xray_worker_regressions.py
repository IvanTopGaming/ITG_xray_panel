import json
import os
import socket
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from flask import Flask

from panel_core.extensions import db
from panel_core.models import Balancer, Client, Inbound, Outbound, RoutingProfile


@pytest.fixture
def worker(monkeypatch, tmp_path):
    stubs = os.environ.get("XRAY_PROTO_PATH", "/tmp/itg-xray-audit.s88BH2/stubs")
    if not Path(stubs).is_dir():
        pytest.skip("Set XRAY_PROTO_PATH to generated pinned Xray protobuf modules")
    monkeypatch.syspath_prepend(stubs)
    from panel_core.xray import engine, grpc_client

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    db.init_app(app)
    monkeypatch.setattr(engine, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(engine, "CANDIDATE_PATH", str(tmp_path / "candidate.json"))
    monkeypatch.setattr(engine, "LOCK_PATH", str(tmp_path / "config.lock"))
    monkeypatch.setattr(engine, "ACCESS_LOG_PATH", str(tmp_path / "access.log"))
    monkeypatch.setattr(engine, "ERROR_LOG_PATH", str(tmp_path / "error.log"))
    monkeypatch.setattr(
        engine,
        "get_system_settings",
        lambda: {"xrayLogLevel": "none", "geoipUrl": "https://fixture/geoip", "geositeUrl": "https://fixture/geosite"},
    )
    with app.app_context():
        db.create_all()
        db.session.add(Outbound(tag="direct"))
        db.session.add(Outbound(tag="block", protocol="blackhole"))
        db.session.commit()
        yield engine, grpc_client
        db.session.remove()
        db.drop_all()


def test_vmess_rpc_encodes_vmess_account(worker, monkeypatch):
    engine, client = worker
    from app.proxyman.command import command_pb2

    db.session.add(Inbound(tag="vmess", port=12000, protocol="vmess", stream_settings="{}"))
    db.session.commit()
    accounts = []

    def alter(request, **kwargs):
        operation = command_pb2.AddUserOperation.FromString(request.operation.value)
        accounts.append(operation.user.account.type)

    monkeypatch.setattr(
        client.command_pb2_grpc, "HandlerServiceStub", lambda channel: SimpleNamespace(AlterInbound=alter)
    )
    monkeypatch.setattr(client, "get_channel", lambda: None)
    user = SimpleNamespace(id=str(uuid.uuid4()), email="person", flow="")
    assert client._api_add_user_grpc("vmess", user)
    assert accounts == ["xray.proxy.vmess.Account"]


def test_existing_user_is_not_treated_as_verified_account(worker, monkeypatch):
    engine, client = worker
    import grpc

    db.session.add(Inbound(tag="vless", port=12000, protocol="vless", stream_settings="{}"))
    db.session.commit()

    def alter(*args, **kwargs):
        raise grpc.RpcError("already exists")

    monkeypatch.setattr(
        client.command_pb2_grpc, "HandlerServiceStub", lambda channel: SimpleNamespace(AlterInbound=alter)
    )
    monkeypatch.setattr(client, "get_channel", lambda: None)
    assert not client._api_add_user_grpc("vless", SimpleNamespace(id=str(uuid.uuid4()), email="person", flow=""))


@pytest.mark.parametrize("method", ["aes-128-gcm", "aes-256-gcm", "chacha20-poly1305"])
def test_classic_shadowsocks_config_passes_real_xray(worker, monkeypatch, method):
    engine, client = worker
    binary = os.environ.get("XRAY_TEST_BINARY", "/tmp/itg-xray-audit.s88BH2/xray")
    if not Path(binary).is_file():
        pytest.skip("Set XRAY_TEST_BINARY to pinned Xray executable")
    monkeypatch.setattr(engine, "XRAY_BIN", binary)
    db.session.add(
        Inbound(
            tag="ss",
            port=12000,
            protocol="shadowsocks",
            stream_settings=json.dumps(
                {"network": "tcp", "security": "none", "ssMethod": method, "ssPassword": "server-pass"}
            ),
        )
    )
    db.session.add(Client(id="client-pass", inbound_tag="ss", email="person", enable=True))
    db.session.commit()
    engine.generate_config_file()
    config = json.loads(Path(engine.CONFIG_PATH).read_text())
    user = config["inbounds"][1]["settings"]["clients"][0]
    assert user["method"] == method


def test_failed_geo_download_preserves_both_working_files(worker, monkeypatch, tmp_path):
    engine, client = worker
    old_ip, old_site = tmp_path / "geoip.dat", tmp_path / "geosite.dat"
    old_ip.write_bytes(b"old-ip")
    old_site.write_bytes(b"old-site")
    real_open = open

    def local_open(path, *args, **kwargs):
        if str(path).startswith("/etc/xray/"):
            path = tmp_path / Path(path).name
        return real_open(path, *args, **kwargs)

    class Response:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, **kwargs):
            yield b"partial"
            if self.url.endswith("geosite"):
                raise requests.ConnectionError("interrupted")

    monkeypatch.setattr(engine, "open", local_open, raising=False)
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: Response(url))
    with pytest.raises(RuntimeError):
        engine.update_geo_db()
    assert old_ip.read_bytes() == b"old-ip"
    assert old_site.read_bytes() == b"old-site"


def test_engine_refuses_missing_profile_target(worker):
    engine, client = worker
    profile = RoutingProfile(name="broken", rules='[{"domain":["example.org"],"outboundTag":"deleted"}]')
    db.session.add(profile)
    db.session.flush()
    db.session.add(
        Inbound(tag="vless", port=12000, protocol="vless", stream_settings="{}", routing_profile_id=profile.id)
    )
    db.session.commit()
    with pytest.raises(ValueError, match="target"):
        engine.generate_config_file(validate=False)


def test_engine_refuses_ambiguous_legacy_balancer_tag(worker):
    engine, client = worker
    db.session.add(Balancer(tag="direct", selector='["direct"]'))
    db.session.commit()
    with pytest.raises(ValueError, match="tag"):
        engine.generate_config_file(validate=False)


def test_preflight_never_publishes_and_nested_engine_lock_does_not_deadlock(worker, monkeypatch):
    engine, client = worker
    from flask import current_app
    from panel_core.services import runtime_apply

    current_app.config["XRAY_CONFIG_LOCK_PATH"] = engine.LOCK_PATH
    monkeypatch.setattr(runtime_apply, "generate_config_file", engine.generate_config_file)
    Path(engine.CONFIG_PATH).write_text('{"previous":"configuration"}')
    with runtime_apply.runtime_lock():
        runtime_apply.prepare_runtime_config()
    assert Path(engine.CONFIG_PATH).read_text() == '{"previous":"configuration"}'
    assert not Path(engine.CANDIDATE_PATH).exists()


@pytest.fixture
def live_xray(worker, monkeypatch, tmp_path, request):
    protocol = request.param
    engine, client = worker
    from flask import current_app

    current_app.config["XRAY_CONFIG_LOCK_PATH"] = engine.LOCK_PATH
    import grpc
    from app.proxyman.command import command_pb2_grpc

    binary = os.environ.get("XRAY_TEST_BINARY", "/tmp/itg-xray-audit.s88BH2/xray")
    if not Path(binary).is_file():
        pytest.skip("Set XRAY_TEST_BINARY to pinned Xray executable")
    monkeypatch.setattr(engine, "XRAY_BIN", binary)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        api_port = listener.getsockname()[1]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        user_port = listener.getsockname()[1]
    config = {
        "log": {"loglevel": "none"},
        "api": {"tag": "api", "services": ["HandlerService", "StatsService"]},
        "stats": {},
        "inbounds": [
            {
                "tag": "api",
                "listen": "127.0.0.1",
                "port": api_port,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            },
            {
                "tag": protocol,
                "listen": "127.0.0.1",
                "port": user_port,
                "protocol": protocol,
                "settings": {"clients": [], "decryption": "none"},
            },
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
        "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]},
    }
    config_path = tmp_path / "rpc.json"
    config_path.write_text(json.dumps(config))
    process = subprocess.Popen(
        [binary, "run", "-c", str(config_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    channel = grpc.insecure_channel(f"127.0.0.1:{api_port}")
    try:
        grpc.channel_ready_future(channel).result(timeout=8)
        monkeypatch.setattr(client, "get_channel", lambda: channel)
        db.session.add(Inbound(tag=protocol, port=user_port, protocol=protocol, stream_settings="{}"))
        db.session.commit()
        stub = command_pb2_grpc.HandlerServiceStub(channel)
        yield protocol, client, stub, process
    finally:
        channel.close()
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("live_xray", ["vless", "vmess"], indirect=True)
def test_real_xray_add_and_remove_account(live_xray):
    from app.proxyman.command import command_pb2

    protocol, client, stub, process = live_xray
    user = SimpleNamespace(id=str(uuid.uuid4()), email="person", flow="")
    assert client._api_add_user_grpc(protocol, user)
    users = stub.GetInboundUsers(command_pb2.GetInboundUserRequest(tag=protocol), timeout=3).users
    assert len(users) == 1
    assert users[0].account.type == f"xray.proxy.{protocol}.Account"
    assert client._api_remove_user_grpc(protocol, "person")
    assert not stub.GetInboundUsers(command_pb2.GetInboundUserRequest(tag=protocol), timeout=3).users


@pytest.mark.parametrize("live_xray", ["vless", "vmess"], indirect=True)
def test_real_xray_provision_and_renew_keep_other_users(live_xray, monkeypatch):
    from app.proxyman.command import command_pb2
    from panel_core.models import ProvisionReceipt, RuntimeApplyState
    from panel_core.services import runtime_apply
    from panel_core.services.provisioning import provision_single_item
    from panel_core.xray import gateway
    from panel_core.xray.local import LocalXrayGateway

    protocol, client, stub, process = live_xray
    monkeypatch.setattr(gateway, "_gateway", LocalXrayGateway())
    monkeypatch.setattr(client, "_runtime_epoch", lambda: str(process.pid))
    unrelated = SimpleNamespace(id=str(uuid.uuid4()), email="unrelated", flow="")
    assert client._api_add_user_grpc(protocol, unrelated)
    db.session.add(Client(id=unrelated.id, email=unrelated.email, inbound_tag=protocol, enable=True))
    db.session.commit()

    def reject_restart():
        raise AssertionError("Provisioning must not restart Xray")

    monkeypatch.setattr(runtime_apply, "restart_xray_container", reject_restart)
    arguments = {
        "telegram_id": 42,
        "inbound_tag": protocol,
        "tariff_id": 1,
        "period_ms": 86400000,
        "limit_bytes": 1000,
    }
    first = provision_single_item(**arguments, idempotency_key="pay:1")
    second = provision_single_item(**arguments, idempotency_key="pay:2")
    replay = provision_single_item(**arguments, idempotency_key="pay:2")

    users = stub.GetInboundUsers(command_pb2.GetInboundUserRequest(tag=protocol), timeout=3).users
    assert len(users) == 2
    account_type = f"xray.proxy.{protocol}.Account"
    assert all(user.account.type == account_type for user in users)
    if protocol == "vless":
        from proxy.vless.account_pb2 import Account
    else:
        from proxy.vmess.account_pb2 import Account
    assert {Account.FromString(user.account.value).id for user in users} == {unrelated.id, first["client"]["id"]}
    assert second["expires_at_ms"] == first["expires_at_ms"] + 86400000
    assert replay["expires_at_ms"] == second["expires_at_ms"]
    assert process.poll() is None
    state = db.session.get(RuntimeApplyState, 1)
    assert state.desired_revision == state.applied_revision
    assert all(receipt.materialized for receipt in ProvisionReceipt.query.all())


@pytest.mark.parametrize("live_xray", ["vless", "vmess"], indirect=True)
@pytest.mark.parametrize("action", ["quota", "expiry", "block", "revoke", "cycle", "source_expiry"])
def test_real_xray_access_lifecycle_keeps_other_users(live_xray, monkeypatch, action):
    from datetime import datetime

    from app.proxyman.command import command_pb2
    from panel_core.services import entitlements, runtime_apply, stats
    from panel_core.services.provisioning import provision_single_item
    from panel_core.xray import gateway
    from panel_core.xray.local import LocalXrayGateway

    protocol, grpc_client, stub, process = live_xray
    monkeypatch.setattr(gateway, "_gateway", LocalXrayGateway())
    monkeypatch.setattr(grpc_client, "_runtime_epoch", lambda: str(process.pid))
    unrelated = Client(id=str(uuid.uuid4()), email="unrelated", inbound_tag=protocol, enable=True)
    db.session.add(unrelated)
    db.session.commit()
    assert grpc_client._api_add_user_grpc(protocol, unrelated)

    def reject_restart():
        raise AssertionError("Changing one user's access must not restart Xray")

    monkeypatch.setattr(runtime_apply, "restart_xray_container", reject_restart)
    arguments = dict(telegram_id=42, inbound_tag=protocol, tariff_id=1, limit_bytes=1000)
    first = provision_single_item(**arguments, expiry_ms=0, operation_id="grant:base", source_id="grant:base")
    client = db.session.get(Client, first["client"]["id"])
    if action in {"quota", "cycle"}:
        client.up = 1000
        db.session.commit()
        stats.check_limits_and_reset()
        if action == "cycle":
            entitlements.reset_source_cycle(
                telegram_id=42,
                inbound_tag=protocol,
                tariff_id=1,
                source_id="grant:base",
                source_revision=0,
                operation_id="cycle:1",
            )
    elif action == "expiry":
        client.expiry_time = 1
        db.session.commit()
        stats.check_limits_and_reset()
    elif action == "block":
        entitlements.apply_account_state(telegram_id=42, revision=1, blocked=True)
        entitlements.apply_account_state(telegram_id=42, revision=1, blocked=True)
        entitlements.apply_account_state(telegram_id=42, revision=2, blocked=False)
    elif action == "revoke":
        entitlements.revoke_source(telegram_id=42, inbound_tag=protocol, source_id="grant:base", tariff_id=1)
    else:
        end = int(datetime.now().timestamp() * 1000) + 100000
        provision_single_item(
            **{**arguments, "limit_bytes": 100}, expiry_ms=end, operation_id="temporary", source_id="temporary"
        )

        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp((end + 1000) / 1000, tz)

        monkeypatch.setattr(stats, "datetime", Later)
        stats.check_limits_and_reset()
        assert client.limit_bytes == 1000

    users = stub.GetInboundUsers(command_pb2.GetInboundUserRequest(tag=protocol), timeout=3).users
    if protocol == "vless":
        from proxy.vless.account_pb2 import Account
    else:
        from proxy.vmess.account_pb2 import Account
    expected = {unrelated.id}
    if action in {"cycle", "block", "source_expiry"}:
        expected.add(client.id)
    assert {Account.FromString(user.account.value).id for user in users} == expected
    assert process.poll() is None


@pytest.mark.parametrize("live_xray", ["vless"], indirect=True)
@pytest.mark.parametrize("action", ["quota", "expiry", "block", "revoke"])
def test_disabling_access_denies_new_connections_and_preserves_existing_streams(live_xray, monkeypatch, action):
    import socketserver
    import struct
    import threading
    from contextlib import ExitStack

    from panel_core.services import entitlements, runtime_apply, stats
    from panel_core.services.provisioning import provision_single_item
    from panel_core.xray import gateway
    from panel_core.xray.local import LocalXrayGateway

    protocol, grpc_client, stub, process = live_xray
    monkeypatch.setattr(gateway, "_gateway", LocalXrayGateway())
    monkeypatch.setattr(grpc_client, "_runtime_epoch", lambda: str(process.pid))

    def reject_restart():
        raise AssertionError("Existing streams must survive disabling another user's credentials")

    monkeypatch.setattr(runtime_apply, "restart_xray_container", reject_restart)
    result = provision_single_item(
        telegram_id=42, inbound_tag=protocol, tariff_id=1, limit_bytes=1000, period_ms=86400000, idempotency_key="pay:1"
    )
    client = db.session.get(Client, result["client"]["id"])
    other = Client(id=str(uuid.uuid4()), email="other", inbound_tag=protocol, enable=True)
    db.session.add(other)
    db.session.commit()
    assert grpc_client._api_add_user_grpc(protocol, other)
    inbound_port = Inbound.query.filter_by(tag=protocol).one().port

    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            while data := self.request.recv(4096):
                self.request.sendall(data)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    def receive(stream, count):
        data = b""
        while len(data) < count:
            chunk = stream.recv(count - len(data))
            assert chunk, "Authenticated stream closed unexpectedly"
            data += chunk
        return data

    with Server(("127.0.0.1", 0), Echo) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def header(identity):
                return (
                    b"\x00"
                    + uuid.UUID(identity).bytes
                    + b"\x00\x01"
                    + struct.pack("!H", server.server_address[1])
                    + b"\x01"
                    + socket.inet_aton("127.0.0.1")
                )

            with ExitStack() as streams:
                current = streams.enter_context(socket.create_connection(("127.0.0.1", inbound_port), timeout=3))
                unrelated = streams.enter_context(socket.create_connection(("127.0.0.1", inbound_port), timeout=3))
                for stream, identity in [(current, client.id), (unrelated, other.id)]:
                    stream.sendall(header(identity) + b"before")
                    assert receive(stream, 8) == b"\x00\x00before"
                if action == "block":
                    entitlements.apply_account_state(telegram_id=42, revision=1, blocked=True)
                elif action == "revoke":
                    entitlements.revoke_source(telegram_id=42, inbound_tag=protocol, source_id="pay:1", tariff_id=1)
                else:
                    if action == "quota":
                        client.up = 1000
                    else:
                        client.expiry_time = 1
                    db.session.commit()
                    stats.check_limits_and_reset()
                assert client.enable is False
                for stream in [current, unrelated]:
                    stream.sendall(b"after")
                    assert receive(stream, 5) == b"after"
                with socket.create_connection(("127.0.0.1", inbound_port), timeout=3) as denied:
                    denied.sendall(header(client.id) + b"denied")
                    try:
                        response = denied.recv(16)
                    except ConnectionResetError:
                        response = b""
                    assert response == b""
                assert process.poll() is None
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("fail_restart", [False, True])
def test_geo_pair_activation_and_restart_rollback(worker, monkeypatch, tmp_path, fail_restart):
    engine, client = worker
    from app.router.config_pb2 import CIDR, Domain, GeoIP, GeoIPList, GeoSite, GeoSiteList

    pair = {
        "geoip": GeoIPList(
            entry=[GeoIP(country_code="ZZ", cidr=[CIDR(ip=b"\x7f\x00\x00\x00", prefix=8)])]
        ).SerializeToString(),
        "geosite": GeoSiteList(
            entry=[GeoSite(country_code="ZZ", domain=[Domain(type=3, value="example.org")])]
        ).SerializeToString(),
    }
    (tmp_path / "geoip.dat").write_bytes(b"old-ip")
    (tmp_path / "geosite.dat").write_bytes(b"old-site")
    binary = os.environ.get("XRAY_TEST_BINARY", "/tmp/itg-xray-audit.s88BH2/xray")
    if not Path(binary).is_file():
        pytest.skip("Set XRAY_TEST_BINARY to pinned Xray executable")
    monkeypatch.setattr(engine, "XRAY_BIN", binary)
    Path(engine.CONFIG_PATH).write_text(
        json.dumps(
            {
                "outbounds": [{"tag": "direct", "protocol": "freedom"}],
                "routing": {
                    "rules": [
                        {"type": "field", "ip": ["geoip:ZZ"], "outboundTag": "direct"},
                        {"type": "field", "domain": ["geosite:ZZ"], "outboundTag": "direct"},
                    ]
                },
            }
        )
    )

    class Response:
        def __init__(self, url):
            self.content = pair[url.rsplit("/", 1)[1]]
            self.headers = {"Content-Length": str(len(self.content))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, **kwargs):
            yield self.content

    restarts = []

    def restart():
        restarts.append(((tmp_path / "geoip.dat").read_bytes(), (tmp_path / "geosite.dat").read_bytes()))
        if fail_restart and len(restarts) == 1:
            raise RuntimeError("restart failed")

    monkeypatch.setattr(requests, "get", lambda url, **kwargs: Response(url))
    monkeypatch.setattr(engine, "restart_xray_container", restart)
    if fail_restart:
        with pytest.raises(RuntimeError):
            engine.update_geo_db()
        assert restarts == [(pair["geoip"], pair["geosite"]), (b"old-ip", b"old-site")]
        assert (tmp_path / "geoip.dat").read_bytes() == b"old-ip"
    else:
        engine.update_geo_db()
        assert restarts == [(pair["geoip"], pair["geosite"])]
        assert (tmp_path / "geoip.dat").is_symlink()
        assert (tmp_path / "geosite.dat").read_bytes() == pair["geosite"]
