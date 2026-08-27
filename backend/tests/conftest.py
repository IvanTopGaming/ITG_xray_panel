import sys
from unittest.mock import MagicMock


_grpc_mock = MagicMock()
_grpc_experimental_mock = MagicMock()
_grpc_experimental_mock.gevent = MagicMock()
_grpc_mock.experimental = _grpc_experimental_mock
sys.modules.setdefault("grpc", _grpc_mock)
sys.modules.setdefault("grpc.experimental", _grpc_experimental_mock)
sys.modules.setdefault("grpc_gevent", MagicMock())


_GRPC_STUBS = [
    "app.proxyman",
    "app.proxyman.command",
    "app.proxyman.command.command_pb2",
    "app.proxyman.command.command_pb2_grpc",
    "app.stats",
    "app.stats.command",
    "app.stats.command.command_pb2",
    "app.stats.command.command_pb2_grpc",
    "common",
    "common.protocol",
    "common.protocol.user_pb2",
    "common.serial",
    "common.serial.typed_message_pb2",
    "proxy",
    "proxy.vless",
    "proxy.vless.account_pb2",
]
for _name in _GRPC_STUBS:
    sys.modules.setdefault(_name, MagicMock())

import os  # noqa: E402


os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only-must-be-long-enough-for-hs256-jwt-signing")
os.environ.setdefault("PANEL_ADMIN_USER", "admin")
os.environ.setdefault("PANEL_ADMIN_PASSWORD", "admin")
os.environ.setdefault("PANEL_DOMAIN", "localhost")
os.environ.setdefault("PROXY_DOMAIN", "localhost")
os.environ.setdefault("PANEL_SECRET_PATH", "/test")
os.environ.setdefault("RATELIMIT_STORAGE_URI", "memory://")

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from panel_core.bootstrap import bootstrap_gevent  # noqa: E402

bootstrap_gevent()

from panel_core.extensions import db as _db  # noqa: E402
import panel_core.models  # noqa: E402, F401  -- registers tables with db.metadata


_PANEL_ROLE_UNSET = object()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    from panel_core.panel_role import ROLE_ENV

    previous = os.environ.get(ROLE_ENV, _PANEL_ROLE_UNSET)
    yield
    if previous is _PANEL_ROLE_UNSET:
        os.environ.pop(ROLE_ENV, None)
    else:
        os.environ[ROLE_ENV] = previous


@pytest.fixture(autouse=True)
def _reset_xray_gateway():

    from panel_core.xray import gateway as _gateway_module
    from panel_core.xray.local import LocalXrayGateway

    _gateway_module.set_default_xray_gateway(LocalXrayGateway())
    _gateway_module.set_xray_gateway(None)
    yield
    _gateway_module.set_xray_gateway(None)
    _gateway_module.set_default_xray_gateway(None)


@pytest.fixture(autouse=True)
def _xray_paths_stay_out_of_etc(tmp_path_factory):
    """Keep the worker's config and lock inside the test's own directory.

    Building the worker role runs bootstrap_defaults, which calls generate_config_file, which takes
    a FileLock on /etc/xray/config.lock -- and filelock creates the parent directory. Under root that
    silently succeeds and the suite writes into the host's /etc; under any other user it raises
    PermissionError, and every test that builds a node fails at setup. That is 35 failures and 132
    errors in CI against a suite that is green locally, purely because the local run happens to be
    root. Redirecting the two paths fixes both halves at once.
    """

    from panel_core.xray import engine

    base = tmp_path_factory.mktemp("xray-etc")
    saved = {name: getattr(engine, name) for name in ("CONFIG_PATH", "LOCK_PATH", "CANDIDATE_PATH")}
    engine.CONFIG_PATH = str(base / "config.json")
    engine.LOCK_PATH = str(base / "config.lock")
    engine.CANDIDATE_PATH = str(base / "config.candidate.json")
    yield
    for name, value in saved.items():
        setattr(engine, name, value)


@pytest.fixture(autouse=True)
def _reset_redis_clients():

    from panel_core.extensions import reset_redis_clients

    reset_redis_clients()
    yield
    reset_redis_clients()


@pytest.fixture(autouse=True)
def _reset_grpc_channel():

    from panel_core.xray import grpc_client as _grpc_client_module

    _grpc_client_module._close_channel()
    yield
    _grpc_client_module._close_channel()


@pytest.fixture(scope="function")
def app():

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        from panel_core.models import FederationConfig
        from panel_core.app_base import register_readyz

        _db.session.add(FederationConfig(id=1))
        _db.session.commit()
        register_readyz(app)
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture(scope="function")
def db(app):

    return _db


def _seed_everything(db):
    import json

    from panel_core.models import (
        Balancer,
        Client,
        Inbound,
        NotificationLog,
        Outbound,
        ProvisionReceipt,
        RoutingProfile,
        SystemSetting,
    )

    profile = RoutingProfile(name="ru", rules='[{"type":"field","outboundTag":"egress-1"}]', enable=True)
    db.session.add(profile)
    db.session.flush()

    db.session.add_all(
        [
            Outbound(tag="direct", protocol="freedom", settings="{}", enable=True),
            Outbound(tag="block", protocol="blackhole", settings="{}", enable=True),
            Outbound(
                tag="egress-1",
                protocol="freedom",
                settings="{}",
                enable=True,
                send_through="172.28.0.130",
                public_ip="203.0.113.7",
                gateway="203.0.113.1",
            ),
            Balancer(tag="bal", enable=True, selector='["direct"]', strategy="random", fallback_tag="direct"),
            Inbound(
                tag="in-reality",
                port=443,
                protocol="vless",
                stream_settings=json.dumps({"security": "reality", "realitySettings": {"privateKey": "SECRET-KEY"}}),
                routing_profile_id=profile.id,
                label="основной",
                up=111,
                down=222,
            ),
            Inbound(tag="in-ws", port=8443, protocol="vmess", stream_settings=json.dumps({"network": "ws"})),
            SystemSetting(key="xray_log_level", value="warning"),
        ]
    )
    db.session.flush()

    db.session.add_all(
        [
            Client(
                id="uuid-1",
                email="a@b",
                inbound_tag="in-reality",
                up=10,
                down=20,
                limit_bytes=1024,
                expiry_time=0,
                last_reset_time=1750000000000,
                source_ips='["1.2.3.4"]',
                telegram_id=777,
                tariff_id=3,
                preferred_outbound="egress-1",
                flow="xtls-rprx-vision",
                enable=True,
            ),
            Client(
                id="uuid-2",
                email="c@d",
                inbound_tag="in-ws",
                up=0,
                down=0,
                expiry_time=1790000000000,
                telegram_id=888,
                enable=False,
            ),
        ]
    )
    db.session.flush()

    db.session.add_all(
        [
            ProvisionReceipt(
                idempotency_key="pay:1",
                inbound_tag="in-reality",
                telegram_id=777,
                response_json='{"expires_at_ms":0}',
                materialized=True,
            ),
            NotificationLog(telegram_id=777, client_id="uuid-1", kind="traffic_80"),
        ]
    )
    db.session.commit()


@pytest.fixture
def rich_node(app, db):
    _seed_everything(db)
    return db
