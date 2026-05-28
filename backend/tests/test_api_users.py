"""Tests for /api/bot/users/* endpoints (admin-side, JWT)."""

import time
from unittest.mock import patch

import jwt as jwt_lib
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.models import (
    Admin,
    Client,
    Inbound,
    Tariff,
    TariffItem,
    TelegramUser,
    UserTariffAccess,
)


class _SqlOrderRecorder:
    """Hook SQLAlchemy's engine to log INSERT/UPDATE/DELETE statements into a
    shared list, so a test can assert that gRPC calls precede every DB write."""

    def __init__(self, order: list[str], label: str = "sql_write"):
        self._order = order
        self._label = label

    def _listener(self, _conn, _cur, statement, *_args):
        head = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if head in ("INSERT", "UPDATE", "DELETE"):
            self._order.append(self._label)

    def __enter__(self):
        event.listen(Engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *_exc):
        event.remove(Engine, "before_cursor_execute", self._listener)


@pytest.fixture
def app_with_admin(app):
    from app.api import bot_admin

    if not any(bp.name == "bot_admin" for bp in app.blueprints.values()):
        app.register_blueprint(bot_admin.bp, url_prefix="/api")
    return app


@pytest.fixture
def admin_headers(app_with_admin, db):
    admin = Admin(username="admin", password="x", password_changed_at=0)
    db.session.add(admin)
    db.session.commit()
    from app.utils import SECRET_KEY

    token = jwt_lib.encode(
        {
            "user": "admin",
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": 0,
            "exp": time.time() + 3600,
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(app_with_admin):
    return app_with_admin.test_client()


@pytest.fixture
def two_inbounds_and_tariff(app_with_admin, db):
    db.session.add(Inbound(tag="DE", protocol="vless", port=10001, stream_settings="{}"))
    db.session.add(Inbound(tag="MSK", protocol="vless", port=10002, stream_settings="{}"))
    db.session.flush()
    t = Tariff(name="Standard", price_rub=150, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="DE", traffic_gb=0, sort_order=0))
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="MSK", traffic_gb=70, sort_order=1))
    db.session.commit()
    return t


@pytest.fixture
def private_tariff(app_with_admin, db, two_inbounds_and_tariff):
    """A second tariff with visibility='private' — needed for paid-grant tests
    (which now require private visibility)."""
    t = Tariff(name="VIP", price_rub=500, period_days=30, visibility="private")
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="DE", traffic_gb=0, sort_order=0))
    db.session.commit()
    return t


# === GET /api/bot/users ===


def test_list_users_empty(app_with_admin, db, client, admin_headers):
    resp = client.get("/api/bot/users", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.get_json() == {"users": []}


def test_list_users_returns_telegram_users(app_with_admin, db, client, admin_headers):
    db.session.add(TelegramUser(telegram_id=42, username="ivan", language="ru"))
    db.session.add(TelegramUser(telegram_id=99, username="anna", language="en"))
    db.session.commit()
    resp = client.get("/api/bot/users", headers=admin_headers)
    body = resp.get_json()
    assert len(body["users"]) == 2
    by_id = {u["telegram_id"]: u for u in body["users"]}
    assert by_id[42]["username"] == "ivan"
    assert by_id[42]["language"] == "ru"
    assert "clients_count" in by_id[42]
    assert "grants_count" in by_id[42]


# === GET /api/bot/users/<id> ===


def test_get_user_404_if_missing(app_with_admin, db, client, admin_headers):
    resp = client.get("/api/bot/users/9999", headers=admin_headers)
    assert resp.status_code == 404


def test_get_user_returns_detail(app_with_admin, db, client, admin_headers):
    db.session.add(TelegramUser(telegram_id=42, username="ivan", language="ru"))
    db.session.commit()
    resp = client.get("/api/bot/users/42", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["telegram_id"] == 42
    assert body["clients"] == []
    assert body["grants"] == []
    assert body["payments"] == []


# === POST /api/bot/users/<id>/grants ===


def test_grant_paid_creates_access_row(app_with_admin, db, client, admin_headers, private_tariff):
    """billing='paid' on a private tariff: creates a UserTariffAccess row,
    does NOT provision Clients, and publishes access_offered."""
    tariff = private_tariff
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()

    with patch("app.api.bot_admin.bot_events.publish") as mock_publish:
        resp = client.post(
            "/api/bot/users/42/grants",
            headers=admin_headers,
            json={"tariff_id": tariff.id, "billing": "paid"},
        )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["telegram_id"] == 42
    assert body["billing"] == "paid"
    assert Client.query.filter_by(telegram_id=42).count() == 0
    assert not any(call.args and call.args[0] == "access_granted" for call in mock_publish.call_args_list)

    offered_calls = [c for c in mock_publish.call_args_list if c.args and c.args[0] == "access_offered"]
    assert len(offered_calls) == 1
    event_type, tg_id, payload = offered_calls[0].args
    assert tg_id == 42
    assert payload["tariff_name"] == tariff.name
    assert payload["lang"] == "ru"
    assert "expires_at_ms" not in payload


def test_grant_paid_rejects_public_tariff(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    """'paid' grants are only meaningful for private tariffs — the user can
    already see and buy public ones without any grant. The API returns 400."""
    tariff = two_inbounds_and_tariff  # default visibility=public
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()

    resp = client.post(
        "/api/bot/users/42/grants",
        headers=admin_headers,
        json={"tariff_id": tariff.id, "billing": "paid"},
    )
    assert resp.status_code == 400
    assert "private" in resp.get_json()["error"].lower()
    assert UserTariffAccess.query.filter_by(telegram_id=42).count() == 0


def test_grant_gift_provisions_once(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    """billing='gift': provisions Clients for one period, leaves
    next_renewal_at=None (no auto-renew), and publishes access_granted_once."""
    tariff = two_inbounds_and_tariff
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()

    with (
        patch("app.services.provisioning._sync_after_provision"),
        patch("app.api.bot_admin.bot_events.publish") as mock_publish,
    ):
        resp = client.post(
            "/api/bot/users/42/grants",
            headers=admin_headers,
            json={"tariff_id": tariff.id, "billing": "gift", "note": "Compensation"},
        )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["billing"] == "gift"
    assert body["next_renewal_at"] is None
    assert Client.query.filter_by(telegram_id=42).count() == 2

    once_calls = [c for c in mock_publish.call_args_list if c.args and c.args[0] == "access_granted_once"]
    assert len(once_calls) == 1
    event_type, tg_id, payload = once_calls[0].args
    assert tg_id == 42
    assert payload["tariff_name"] == tariff.name
    assert payload["lang"] == "ru"
    assert isinstance(payload["expires_at_ms"], int) and payload["expires_at_ms"] > 0

    # No competing event types
    assert not any(call.args and call.args[0] == "access_granted" for call in mock_publish.call_args_list)
    assert not any(call.args and call.args[0] == "access_offered" for call in mock_publish.call_args_list)


def test_grant_free_provisions_immediately(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    """billing='free' provisions Clients immediately, sets next_renewal_at,
    and publishes an access_granted event so the bot DMs the user."""
    tariff = two_inbounds_and_tariff
    db.session.add(TelegramUser(telegram_id=42, language="en"))
    db.session.commit()

    with (
        patch("app.services.provisioning._sync_after_provision"),
        patch("app.api.bot_admin.bot_events.publish") as mock_publish,
    ):
        resp = client.post(
            "/api/bot/users/42/grants",
            headers=admin_headers,
            json={"tariff_id": tariff.id, "billing": "free", "note": "VIP"},
        )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["billing"] == "free"
    assert body["next_renewal_at"] is not None
    assert Client.query.filter_by(telegram_id=42).count() == 2

    grant_calls = [c for c in mock_publish.call_args_list if c.args and c.args[0] == "access_granted"]
    assert len(grant_calls) == 1
    event_type, tg_id, payload = grant_calls[0].args
    assert tg_id == 42
    assert payload["tariff_name"] == tariff.name
    assert payload["lang"] == "en"
    assert isinstance(payload["expires_at_ms"], int) and payload["expires_at_ms"] > 0
    assert not any(call.args and call.args[0] == "access_offered" for call in mock_publish.call_args_list)


def test_grant_rejects_invalid_billing(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    tariff = two_inbounds_and_tariff
    resp = client.post(
        "/api/bot/users/42/grants",
        headers=admin_headers,
        json={"tariff_id": tariff.id, "billing": "comp"},
    )
    assert resp.status_code == 400


def test_grant_rejects_unknown_tariff(app_with_admin, db, client, admin_headers):
    resp = client.post(
        "/api/bot/users/42/grants",
        headers=admin_headers,
        json={"tariff_id": 9999, "billing": "free"},
    )
    assert resp.status_code == 404


def test_grant_upsert_replaces_existing(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    """Re-granting the same (tg_id, tariff_id) updates the existing row."""
    tariff = two_inbounds_and_tariff
    db.session.add(UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="paid"))
    db.session.commit()

    with patch("app.services.provisioning._sync_after_provision"):
        resp = client.post(
            "/api/bot/users/42/grants",
            headers=admin_headers,
            json={"tariff_id": tariff.id, "billing": "free"},
        )
    assert resp.status_code == 201
    rows = UserTariffAccess.query.filter_by(telegram_id=42).all()
    assert len(rows) == 1
    assert rows[0].billing == "free"


# === DELETE /api/bot/users/<id>/grants/<grant_id> ===


def test_revoke_grant(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    tariff = two_inbounds_and_tariff
    grant = UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="paid")
    db.session.add(grant)
    db.session.commit()

    resp = client.delete(f"/api/bot/users/42/grants/{grant.id}", headers=admin_headers)
    assert resp.status_code == 200
    assert db.session.get(UserTariffAccess, grant.id) is None


def test_revoke_grant_404_if_missing(app_with_admin, db, client, admin_headers):
    resp = client.delete("/api/bot/users/42/grants/9999", headers=admin_headers)
    assert resp.status_code == 404


# === GET /api/bot/grants ===


def test_list_grants(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    """Granted-tab endpoint returns all grants (both 'free' and 'paid'),
    each row carrying its own `billing` field."""
    tariff = two_inbounds_and_tariff
    db.session.add(TelegramUser(telegram_id=42, username="ivan", language="ru"))
    db.session.add(
        UserTariffAccess(
            telegram_id=42,
            tariff_id=tariff.id,
            billing="free",
            note="VIP",
        )
    )
    db.session.add(
        UserTariffAccess(
            telegram_id=99,
            tariff_id=tariff.id,
            billing="paid",
        )
    )
    db.session.commit()
    resp = client.get("/api/bot/grants", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["rows"]) == 2

    by_tg = {row["telegram_id"]: row for row in body["rows"]}
    assert by_tg[42]["billing"] == "free"
    assert by_tg[42]["tariff_name"] == "Standard"
    assert by_tg[42]["note"] == "VIP"
    assert by_tg[99]["billing"] == "paid"


# === DELETE /api/bot/users/<id>/tariffs/<tariff_id> ===


def _make_user_with_active_clients(db, tariff, telegram_id=42):
    """Helper: create a TelegramUser and two enabled Clients tied to `tariff`,
    one per inbound, returning the list of clients in deterministic order."""
    db.session.add(TelegramUser(telegram_id=telegram_id, language="ru"))
    db.session.flush()
    c1 = Client(
        id="11111111-1111-1111-1111-111111111111",
        inbound_tag="DE",
        email=f"tg{telegram_id}_DE",
        telegram_id=telegram_id,
        tariff_id=tariff.id,
        enable=True,
        expiry_time=0,
    )
    c2 = Client(
        id="22222222-2222-2222-2222-222222222222",
        inbound_tag="MSK",
        email=f"tg{telegram_id}_MSK",
        telegram_id=telegram_id,
        tariff_id=tariff.id,
        enable=True,
        expiry_time=0,
    )
    db.session.add_all([c1, c2])
    db.session.commit()
    return [c1, c2]


def test_revoke_tariff_disables_clients_and_removes_grant(
    app_with_admin, db, client, admin_headers, two_inbounds_and_tariff
):
    tariff = two_inbounds_and_tariff
    _make_user_with_active_clients(db, tariff)
    db.session.add(UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="free"))
    db.session.commit()

    with (
        patch("app.api.bot_admin._api_remove_user_grpc", return_value=True) as remove,
        patch("app.api.bot_admin.generate_config_file") as regen,
        patch("app.api.bot_admin.restart_xray_container") as restart,
    ):
        resp = client.delete(f"/api/bot/users/42/tariffs/{tariff.id}", headers=admin_headers)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body == {
        "ok": True,
        "telegram_id": 42,
        "tariff_id": tariff.id,
        "disabled_clients": 2,
        "revoked_grants": 1,
    }

    remaining = Client.query.filter_by(telegram_id=42).all()
    assert len(remaining) == 2
    assert all(c.enable is False for c in remaining)
    assert all(c.tariff_id is None for c in remaining)
    assert UserTariffAccess.query.filter_by(telegram_id=42, tariff_id=tariff.id).count() == 0

    # vless inbounds → one gRPC remove per disabled client, no container restart.
    assert remove.call_count == 2
    regen.assert_called_once()
    restart.assert_not_called()


def test_revoke_tariff_idempotent_when_nothing_to_revoke(
    app_with_admin, db, client, admin_headers, two_inbounds_and_tariff
):
    tariff = two_inbounds_and_tariff
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()

    with (
        patch("app.api.bot_admin._api_remove_user_grpc") as remove,
        patch("app.api.bot_admin.generate_config_file") as regen,
        patch("app.api.bot_admin.restart_xray_container") as restart,
    ):
        resp = client.delete(f"/api/bot/users/42/tariffs/{tariff.id}", headers=admin_headers)

    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True,
        "telegram_id": 42,
        "tariff_id": tariff.id,
        "disabled_clients": 0,
        "revoked_grants": 0,
    }
    remove.assert_not_called()
    regen.assert_not_called()
    restart.assert_not_called()


def test_revoke_tariff_restarts_when_grpc_fails(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    """If gRPC remove returns False even once, fall back to a container restart."""
    tariff = two_inbounds_and_tariff
    _make_user_with_active_clients(db, tariff)
    db.session.add(UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="paid"))
    db.session.commit()

    with (
        patch("app.api.bot_admin._api_remove_user_grpc", side_effect=[False, True]),
        patch("app.api.bot_admin.generate_config_file") as regen,
        patch("app.api.bot_admin.restart_xray_container") as restart,
    ):
        resp = client.delete(f"/api/bot/users/42/tariffs/{tariff.id}", headers=admin_headers)

    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True,
        "telegram_id": 42,
        "tariff_id": tariff.id,
        "disabled_clients": 2,
        "revoked_grants": 1,
    }
    remaining = Client.query.filter_by(telegram_id=42).all()
    assert all(c.enable is False for c in remaining)
    assert all(c.tariff_id is None for c in remaining)
    assert UserTariffAccess.query.filter_by(telegram_id=42, tariff_id=tariff.id).count() == 0
    regen.assert_called_once()
    restart.assert_called_once()


def test_revoke_tariff_restarts_for_non_vless_protocol(app_with_admin, db, client, admin_headers):
    """Trojan/SS/etc. can't be hot-removed via gRPC — must restart."""
    db.session.add(Inbound(tag="TR", protocol="trojan", port=10003, stream_settings="{}"))
    db.session.flush()
    t = Tariff(name="Trojan-only", price_rub=100, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="TR", traffic_gb=0, sort_order=0))
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.flush()
    db.session.add(
        Client(
            id="33333333-3333-3333-3333-333333333333",
            inbound_tag="TR",
            email="tg42_TR",
            telegram_id=42,
            tariff_id=t.id,
            enable=True,
            expiry_time=0,
        )
    )
    db.session.commit()

    with (
        patch("app.api.bot_admin._api_remove_user_grpc") as remove,
        patch("app.api.bot_admin.generate_config_file") as regen,
        patch("app.api.bot_admin.restart_xray_container") as restart,
    ):
        resp = client.delete(f"/api/bot/users/42/tariffs/{t.id}", headers=admin_headers)

    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True,
        "telegram_id": 42,
        "tariff_id": t.id,
        "disabled_clients": 1,
        "revoked_grants": 0,
    }
    # Non-vless protocol → gRPC remove is skipped entirely.
    remove.assert_not_called()
    regen.assert_called_once()
    restart.assert_called_once()


# === POST /api/bot/users/<id>/block — must kill live Xray sessions ===


def test_block_user_disables_and_removes_via_grpc(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    """Blocking a user must NOT just flip enable=False in the DB — the live
    Xray sessions for that user have to be torn down too, otherwise the
    user keeps streaming until Xray restarts for an unrelated reason."""
    tariff = two_inbounds_and_tariff
    _make_user_with_active_clients(db, tariff)
    db.session.add(UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="free"))
    db.session.commit()

    with (
        patch("app.api.bot_admin._api_remove_user_grpc", return_value=True) as remove,
        patch("app.api.bot_admin.generate_config_file") as regen,
        patch("app.api.bot_admin.restart_xray_container") as restart,
        patch("app.api.bot_admin.bot_events.publish"),
    ):
        resp = client.post("/api/bot/users/42/block", headers=admin_headers)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["telegram_id"] == 42
    assert body["disabled_clients"] == 2
    assert body["cancelled_grants"] == 1

    remaining = Client.query.filter_by(telegram_id=42).all()
    assert all(c.enable is False for c in remaining)
    assert UserTariffAccess.query.filter_by(telegram_id=42).count() == 0

    assert remove.call_count == 2
    regen.assert_called_once()
    restart.assert_not_called()


def test_block_user_restarts_when_grpc_fails(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    """A single gRPC removal failure escalates to a full Xray restart so
    no zombie session survives."""
    tariff = two_inbounds_and_tariff
    _make_user_with_active_clients(db, tariff)
    db.session.commit()

    with (
        patch("app.api.bot_admin._api_remove_user_grpc", side_effect=[False, True]),
        patch("app.api.bot_admin.generate_config_file") as regen,
        patch("app.api.bot_admin.restart_xray_container") as restart,
        patch("app.api.bot_admin.bot_events.publish"),
    ):
        resp = client.post("/api/bot/users/42/block", headers=admin_headers)

    assert resp.status_code == 200
    regen.assert_called_once()
    restart.assert_called_once()


def test_block_user_restarts_for_non_vless_protocol(app_with_admin, db, client, admin_headers):
    """trojan / shadowsocks etc. can't be hot-removed via gRPC → restart."""
    db.session.add(Inbound(tag="TR", protocol="trojan", port=10003, stream_settings="{}"))
    db.session.flush()
    t = Tariff(name="Trojan-only", price_rub=100, period_days=30)
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="TR", traffic_gb=0))
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.add(
        Client(
            id="44444444-4444-4444-4444-444444444444",
            inbound_tag="TR",
            email="tg42_TR",
            telegram_id=42,
            tariff_id=t.id,
            enable=True,
            expiry_time=0,
        )
    )
    db.session.commit()

    with (
        patch("app.api.bot_admin._api_remove_user_grpc") as remove,
        patch("app.api.bot_admin.generate_config_file") as regen,
        patch("app.api.bot_admin.restart_xray_container") as restart,
        patch("app.api.bot_admin.bot_events.publish"),
    ):
        resp = client.post("/api/bot/users/42/block", headers=admin_headers)

    assert resp.status_code == 200
    remove.assert_not_called()
    regen.assert_called_once()
    restart.assert_called_once()


def test_block_user_no_clients_skips_xray_touch(app_with_admin, db, client, admin_headers):
    """User with no active Clients: 200 OK, no Xray touch, idempotent."""
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()

    with (
        patch("app.api.bot_admin._api_remove_user_grpc") as remove,
        patch("app.api.bot_admin.generate_config_file") as regen,
        patch("app.api.bot_admin.restart_xray_container") as restart,
        patch("app.api.bot_admin.bot_events.publish"),
    ):
        resp = client.post("/api/bot/users/42/block", headers=admin_headers)

    assert resp.status_code == 200
    assert resp.get_json()["disabled_clients"] == 0
    remove.assert_not_called()
    regen.assert_not_called()
    restart.assert_not_called()


# === Transaction-shape invariants =============================================
#
# block_user and revoke_tariff_from_user used to mix _api_remove_user_grpc calls
# with autoflushed UPDATE/DELETE statements inside a single open SQLite write
# transaction, holding the writer lock for the entire loop. These tests pin the
# fix: gRPC must complete before the first DB write.


def test_block_user_grpc_calls_precede_all_sql_writes(
    app_with_admin, db, client, admin_headers, two_inbounds_and_tariff
):
    tariff = two_inbounds_and_tariff
    _make_user_with_active_clients(db, tariff)
    db.session.add(UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="free"))
    db.session.commit()

    order: list[str] = []

    def _on_grpc(*_a, **_kw):
        order.append("grpc")
        return True

    with (
        _SqlOrderRecorder(order),
        patch("app.api.bot_admin._api_remove_user_grpc", side_effect=_on_grpc),
        patch("app.api.bot_admin.generate_config_file"),
        patch("app.api.bot_admin.restart_xray_container"),
        patch("app.api.bot_admin.bot_events.publish"),
    ):
        resp = client.post("/api/bot/users/42/block", headers=admin_headers)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    grpc_indices = [i for i, op in enumerate(order) if op == "grpc"]
    write_indices = [i for i, op in enumerate(order) if op == "sql_write"]
    assert grpc_indices, f"No gRPC calls recorded — fixture should hit two vless clients. Order: {order}"
    assert write_indices, f"No SQL writes recorded — block_user should commit user.blocked=True. Order: {order}"
    assert max(grpc_indices) < min(write_indices), (
        f"gRPC call at {max(grpc_indices)} ran after first SQL write at {min(write_indices)}. "
        f"This holds the SQLite write lock across gRPC, blocking concurrent writers. Order: {order}"
    )


def test_revoke_tariff_grpc_calls_precede_all_sql_writes(
    app_with_admin, db, client, admin_headers, two_inbounds_and_tariff
):
    tariff = two_inbounds_and_tariff
    _make_user_with_active_clients(db, tariff)
    db.session.add(UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="free"))
    db.session.commit()

    order: list[str] = []

    def _on_grpc(*_a, **_kw):
        order.append("grpc")
        return True

    with (
        _SqlOrderRecorder(order),
        patch("app.api.bot_admin._api_remove_user_grpc", side_effect=_on_grpc),
        patch("app.api.bot_admin.generate_config_file"),
        patch("app.api.bot_admin.restart_xray_container"),
    ):
        resp = client.delete(f"/api/bot/users/42/tariffs/{tariff.id}", headers=admin_headers)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    grpc_indices = [i for i, op in enumerate(order) if op == "grpc"]
    write_indices = [i for i, op in enumerate(order) if op == "sql_write"]
    assert grpc_indices, f"No gRPC calls recorded — fixture has two vless clients. Order: {order}"
    assert write_indices, f"No SQL writes recorded — revoke should update clients + delete grant. Order: {order}"
    assert max(grpc_indices) < min(write_indices), (
        f"gRPC call at {max(grpc_indices)} ran after first SQL write at {min(write_indices)}. "
        f"This holds the SQLite write lock across gRPC, blocking concurrent writers. Order: {order}"
    )
