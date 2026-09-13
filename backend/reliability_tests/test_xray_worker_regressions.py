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


@pytest.mark.parametrize("protocol", ["vless", "vmess"])
def test_real_xray_add_and_remove_account(worker, monkeypatch, tmp_path, protocol):
    engine, client = worker
    import grpc
    from app.proxyman.command import command_pb2, command_pb2_grpc

    binary = os.environ.get("XRAY_TEST_BINARY", "/tmp/itg-xray-audit.s88BH2/xray")
    if not Path(binary).is_file():
        pytest.skip("Set XRAY_TEST_BINARY to pinned Xray executable")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        api_port = listener.getsockname()[1]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        user_port = listener.getsockname()[1]
    config = {
        "log": {"loglevel": "none"},
        "api": {"tag": "api", "services": ["HandlerService"]},
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
        user = SimpleNamespace(id=str(uuid.uuid4()), email="person", flow="")
        assert client._api_add_user_grpc(protocol, user)
        stub = command_pb2_grpc.HandlerServiceStub(channel)
        users = stub.GetInboundUsers(command_pb2.GetInboundUserRequest(tag=protocol), timeout=3).users
        assert len(users) == 1
        assert users[0].account.type == f"xray.proxy.{protocol}.Account"
        assert client._api_remove_user_grpc(protocol, "person")
        assert not stub.GetInboundUsers(command_pb2.GetInboundUserRequest(tag=protocol), timeout=3).users
    finally:
        channel.close()
        process.terminate()
        process.wait(timeout=5)


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
