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

from app.extensions import db as _db  # noqa: E402
import app.models  # noqa: E402, F401  -- registers tables with db.metadata


@pytest.fixture(scope="function")
def app():

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    _db.init_app(app)
    with app.app_context():
        _db.create_all()
        from app.models import FederationConfig

        _db.session.add(FederationConfig(id=1))
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture(scope="function")
def db(app):

    return _db
