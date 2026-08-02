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
