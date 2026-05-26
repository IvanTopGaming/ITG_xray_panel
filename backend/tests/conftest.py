"""Shared pytest fixtures for backend tests.

The backend's `create_app()` factory eagerly imports services that depend on
gRPC-generated stubs (`app.proxyman.command.command_pb2`, etc.). Those stubs
are produced at Docker build time by `backend/Dockerfile` running
`python -m grpc_tools.protoc` against Xray-core's `.proto` files. They are
not present on a dev checkout.

For model and migration tests we don't need the gRPC machinery to actually
function — we just need the imports to succeed. This conftest stubs the
generated modules in `sys.modules` BEFORE any app imports run, then sets up
a minimal Flask app (no scheduler, no blueprints) for SQLAlchemy operations.

Tests that touch real gRPC behaviour (none in phase 0) would need to run
inside the backend Docker container, or require a future
`backend/scripts/gen_proto.sh` to populate the stubs locally.
"""

import sys
from unittest.mock import MagicMock

# grpc package — required by app/__init__.py (`from grpc.experimental import gevent`).
# grpcio is not installed on dev checkouts; stub it before any app import runs.
_grpc_mock = MagicMock()
_grpc_experimental_mock = MagicMock()
_grpc_experimental_mock.gevent = MagicMock()
_grpc_mock.experimental = _grpc_experimental_mock
sys.modules.setdefault("grpc", _grpc_mock)
sys.modules.setdefault("grpc.experimental", _grpc_experimental_mock)
sys.modules.setdefault("grpc_gevent", MagicMock())

# Generated gRPC stubs — must be in sys.modules before `import app` runs.
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

# Env defaults that `app.create_app()` reads at import time
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")
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
    """Minimal Flask app with in-memory SQLite — no scheduler, no blueprints.

    Sufficient for unit-testing SQLAlchemy models without booting the full
    create_app() factory (which pulls in services + gRPC + APScheduler).
    """
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
    """SQLAlchemy db handle bound to the test app context."""
    return _db
