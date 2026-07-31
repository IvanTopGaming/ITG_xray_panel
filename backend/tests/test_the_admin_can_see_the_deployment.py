"""§10.8: five things an admin used to learn from a user complaint.

Metrics went in phase 6, logs are json-file rotations on five machines collected nowhere, and
`/healthz` / `/readyz` answer "the process is up" and "its database replies". Between those and a
real outage there was nothing: the certificate that has to be renewed by hand on four-plus hosts and
is dated nowhere, the `bot_event` backlog that is the only sign the bus is broken, the payment
stranded in `processing` with money taken and access not granted, the neighbour left behind in a
lock-step release, and the data tier itself.

Two mechanisms, and the split is deliberate. Four readings are things *this* host can look at
directly, so they are a request. The fifth cannot be looked at at all -- sub, bot-api and cron have
no UI, and a node's card shows online/offline and no version -- so each role stamps its own version
into the shared Redis, the same shape wave 5b built for the bot (§67) and for the same reason: the
writer and the reader are different containers, so nothing held in a process can carry it.

**Nothing here may raise.** A health card that 500s because it could not read a certificate tells an
admin less than one that says the certificate could not be read, and it would take About down with
it. Every reading therefore has an unavailable form, and this file asserts the unavailable forms as
hard as the happy ones -- they are what actually ships on a host with no certs mounted.
"""

from __future__ import annotations

import datetime
import importlib
import json
import time

import jwt as jwt_lib
import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Admin, BotEvent, Payment
from panel_core.services import role_status
from panel_core.utils import SECRET_KEY

from tests.schema import ensure_schema


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    try:
        scheduler.remove_all_jobs()
    except Exception:
        pass


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.pings = 0

    def setex(self, key, ttl, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def ping(self):
        self.pings += 1
        return True


@pytest.fixture
def shared(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(role_status, "get_shared_redis", lambda: fake)
    from panel_core.services import health as health_module

    monkeypatch.setattr(health_module, "get_shared_redis", lambda: fake)
    role_status.reset_stamp_throttle()
    yield fake
    role_status.reset_stamp_throttle()


@pytest.fixture
def master(monkeypatch, tmp_path):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/master.db"))
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.com")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)
    app = importlib.import_module("panel_core.roles.master").create_app()
    with app.app_context():
        if Admin.query.first() is None:
            db.session.add(Admin(username="admin", password="x", password_changed_at=0))
            db.session.commit()
    yield app
    _reset_scheduler()


@pytest.fixture
def headers(master):
    with master.app_context():
        admin = Admin.query.first()
        payload = {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": int(admin.password_changed_at or 0),
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
        }
    return {"Authorization": f"Bearer {jwt_lib.encode(payload, SECRET_KEY, algorithm='HS256')}"}


# ---------------------------------------------------------------- neighbours' versions


def test_a_role_stamps_its_version_where_another_container_can_read_it(shared):
    assert role_status.record_role_version("sub", "2.4.13") is True
    assert role_status.get_role_versions()["sub"]["version"] == "2.4.13"


def test_the_stamp_is_throttled(shared):
    """Every stack healthchecks its backend, so an unthrottled stamp is a write every few seconds."""

    assert role_status.record_role_version("sub", "2.4.13", now=1000.0) is True
    assert role_status.record_role_version("sub", "2.4.13", now=1000.0 + 5) is False
    assert role_status.record_role_version("sub", "2.4.13", now=1000.0 + role_status.STAMP_EVERY_S + 1) is True


def test_a_role_that_stops_reporting_disappears_rather_than_going_stale(shared):
    """ "Reporting version X" is a claim with a timestamp; "was on X once" is not worth showing."""

    role_status.record_role_version("cron", "2.4.12")
    stale = json.dumps({"version": "2.4.12", "reported_at": time.time() - role_status.DEFAULT_FRESHNESS_S - 10})
    shared.values[role_status.status_key("cron")] = stale.encode()

    assert "cron" not in role_status.get_role_versions()


def test_an_unreachable_shared_tier_reports_nothing_rather_than_raising(monkeypatch):
    monkeypatch.setattr(role_status, "get_shared_redis", lambda: None)
    assert role_status.get_role_versions() == {}
    assert role_status.record_role_version("sub", "2.4.13") is False


def test_the_master_serves_what_the_neighbours_reported(master, headers, shared):
    role_status.record_role_version("sub", "2.4.13")
    role_status.reset_stamp_throttle()
    role_status.record_role_version("bot_api", "2.4.12")

    body = master.test_client().get("/api/system/version", headers=headers).get_json()

    roles = body["running"]["roles"]
    assert roles["sub"]["version"] == "2.4.13"
    assert roles["bot_api"]["version"] == "2.4.12", (
        "the versions of the hosts with no UI are still invisible, which is the whole of this half: a "
        "host left behind in a lock-step release announces itself by corrupting data, not by being seen."
    )


def test_serving_a_request_stamps_this_role(master, headers, shared):
    """The refresh rides the request path — every stack healthchecks its own backend."""

    master.test_client().get("/healthz")

    assert role_status.status_key("master") in shared.values, (
        "nothing stamps this role's version, so a master is invisible to any panel but its own."
    )


# ---------------------------------------------------------------- the four local readings


def test_health_answers_with_every_reading(master, headers, shared):
    body = master.test_client().get("/api/system/health", headers=headers).get_json()

    assert set(body) == {"certificate", "undelivered_events", "stuck_payments", "data_tier"}


def test_an_absent_certificate_is_reported_not_raised(master, headers, shared, monkeypatch):
    from panel_core.services import health as health_module

    monkeypatch.setattr(health_module, "CERT_PATH", "/nonexistent/fullchain.pem")
    response = master.test_client().get("/api/system/health", headers=headers)

    assert response.status_code == 200, "an unreadable certificate took the whole About card down"
    assert response.get_json()["certificate"] == {"available": False, "reason": "not mounted"}


def test_a_real_certificate_is_dated(master, headers, shared, monkeypatch, tmp_path):
    cert_path = tmp_path / "fullchain.pem"
    cert_path.write_bytes(_self_signed(b"panel.example.com"))
    from panel_core.services import health as health_module

    monkeypatch.setattr(health_module, "CERT_PATH", str(cert_path))

    cert = master.test_client().get("/api/system/health", headers=headers).get_json()["certificate"]

    assert cert["available"] is True
    assert cert["not_after_ms"] > int(time.time() * 1000)
    assert cert["domains"] == ["panel.example.com"]


def test_a_damaged_certificate_is_reported_not_raised(master, headers, shared, monkeypatch, tmp_path):
    broken = tmp_path / "broken.pem"
    broken.write_bytes(b"-----BEGIN CERTIFICATE-----\nnot base64 at all\n-----END CERTIFICATE-----\n")
    from panel_core.services import health as health_module

    monkeypatch.setattr(health_module, "CERT_PATH", str(broken))

    response = master.test_client().get("/api/system/health", headers=headers)
    assert response.status_code == 200
    assert response.get_json()["certificate"] == {"available": False, "reason": "unreadable"}


def test_the_bus_backlog_is_counted(master, headers, shared):
    with master.app_context():
        db.session.add(BotEvent(type="payment_succeeded", telegram_id=1, payload={}))
        delivered = BotEvent(type="payment_succeeded", telegram_id=2, payload={})
        delivered.delivered_at = datetime.datetime.utcnow()
        db.session.add(delivered)
        db.session.commit()

    body = master.test_client().get("/api/system/health", headers=headers).get_json()

    assert body["undelivered_events"] == {"available": True, "count": 1}, (
        "the undelivered count is the only indicator that the bus is broken — a PUBLISH with no "
        "subscriber succeeds, so nothing else notices."
    )


def test_a_stranded_payment_is_counted(master, headers, shared):
    with master.app_context():
        stuck = Payment(
            yookassa_id="yk-1",
            telegram_id=1,
            tariff_id=1,
            tariff_snapshot={},
            amount_rub=1,
            status="processing",
        )
        db.session.add(stuck)
        old = Payment(
            yookassa_id="yk-2",
            telegram_id=1,
            tariff_id=1,
            tariff_snapshot={},
            amount_rub=1,
            status="pending",
        )
        db.session.add(old)
        db.session.flush()
        old.created_at = datetime.datetime.utcnow() - datetime.timedelta(hours=30)
        db.session.commit()

    body = master.test_client().get("/api/system/health", headers=headers).get_json()

    assert body["stuck_payments"]["processing"] == 1
    assert body["stuck_payments"]["pending_over_a_day"] == 1


def test_the_data_tier_is_probed(master, headers, shared):
    body = master.test_client().get("/api/system/health", headers=headers).get_json()

    assert body["data_tier"] == {"database": "ok", "shared_redis": "ok"}
    assert shared.pings >= 1, "the Redis line was answered without asking the Redis anything"


def test_an_unreachable_shared_redis_is_reported_not_raised(master, headers, monkeypatch):
    class DeadRedis:
        def ping(self):
            raise ConnectionError("connection refused")

    from panel_core.services import health as health_module

    monkeypatch.setattr(health_module, "get_shared_redis", lambda: DeadRedis())
    response = master.test_client().get("/api/system/health", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["data_tier"]["shared_redis"] == "down"


def test_health_needs_an_admin(master):
    assert master.test_client().get("/api/system/health").status_code == 401


def _self_signed(common_name: bytes) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name.decode())])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=42))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name.decode())]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)
