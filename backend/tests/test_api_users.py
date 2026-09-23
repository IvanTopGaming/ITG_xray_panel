import time
from unittest.mock import MagicMock, call, patch

import jwt as jwt_lib
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from panel_core.models import (
    Admin,
    Client,
    Inbound,
    LinkedPanel,
    Tariff,
    TariffItem,
    TelegramUser,
    UserTariffAccess,
    ProvisionOperation,
)


class _SqlOrderRecorder:
    def __init__(self, order: list[str], label: str = "sql_write"):
        self._order = order
        self._label = label

    def _listener(self, _conn, _cur, statement, *_args):
        head = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if head in ("INSERT", "UPDATE", "DELETE"):
            self._order.append(self._label)

    def _commit(self, connection):
        self._order.append("commit")

    def __enter__(self):
        event.listen(Engine, "commit", self._commit)
        event.listen(Engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *_exc):
        event.remove(Engine, "commit", self._commit)
        event.remove(Engine, "before_cursor_execute", self._listener)


def _published(mock):
    return [call(item.args[0].type, item.args[0].telegram_id, item.args[0].payload) for item in mock.call_args_list]


@pytest.fixture(autouse=True)
def runtime_io(monkeypatch):
    generated, restarted = MagicMock(), MagicMock()
    monkeypatch.setattr("panel_core.services.runtime_apply.generate_config_file", generated)
    monkeypatch.setattr("panel_core.services.runtime_apply.restart_xray_container", restarted)
    monkeypatch.setattr("panel_core.services.bot_events.publish_stored", MagicMock())
    return generated, restarted


@pytest.fixture
def app_with_admin(app):
    from panel_core.api import bot_admin

    if not any(bp.name == "bot_admin" for bp in app.blueprints.values()):
        app.register_blueprint(bot_admin.bp, url_prefix="/api")
    return app


@pytest.fixture
def admin_headers(app_with_admin, db):
    admin = Admin(username="admin", password="x", password_changed_at=0)
    db.session.add(admin)
    db.session.commit()
    from panel_core.utils import SECRET_KEY

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

    t = Tariff(name="VIP", price_rub=500, period_days=30, visibility="private")
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="DE", traffic_gb=0, sort_order=0))
    db.session.commit()
    return t


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


def test_grant_paid_creates_access_row(app_with_admin, db, client, admin_headers, private_tariff):

    tariff = private_tariff
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()

    with patch("panel_core.services.bot_events.publish_stored") as mock_publish:
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
    assert not any(call.args and call.args[0] == "access_granted" for call in _published(mock_publish))

    offered_calls = [c for c in _published(mock_publish) if c.args and c.args[0] == "access_offered"]
    assert len(offered_calls) == 1
    event_type, tg_id, payload = offered_calls[0].args
    assert tg_id == 42
    assert payload["tariff_name"] == tariff.name
    assert payload["lang"] == "ru"
    assert "expires_at_ms" not in payload


def test_grant_paid_rejects_public_tariff(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):

    tariff = two_inbounds_and_tariff
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


def test_grant_with_a_term_provisions_and_announces_access(
    app_with_admin, db, client, admin_headers, two_inbounds_and_tariff
):
    """What `gift` used to be: access that ends on a date and is not renewed.

    It differed from `free` only in which machine kept it alive -- the cron renewed one and left the
    other to lapse -- so it is now the same grant with a date in `access_until`, and it announces
    itself with the same `access_granted` every other issued access uses.
    """

    tariff = two_inbounds_and_tariff
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()

    with (
        patch("panel_core.services.runtime_apply.generate_config_file"),
        patch("panel_core.services.bot_events.publish_stored") as mock_publish,
    ):
        resp = client.post(
            "/api/bot/users/42/grants",
            headers=admin_headers,
            json={
                "tariff_id": tariff.id,
                "billing": "free",
                "access_until": "2027-03-01T00:00:00",
                "note": "Compensation",
            },
        )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["billing"] == "free"
    assert body["access_until"].startswith("2027-03-01"), (
        f"the term the admin typed is the grant's own state, not a side effect of the key; got {body!r}"
    )
    assert Client.query.filter_by(telegram_id=42).count() == 2

    granted = [c for c in _published(mock_publish) if c.args and c.args[0] == "access_granted"]
    assert len(granted) == 1, f"issued access announces itself exactly once; got {_published(mock_publish)!r}"
    _event_type, tg_id, payload = granted[0].args
    assert tg_id == 42
    assert payload["tariff_name"] == tariff.name
    assert payload["lang"] == "ru"

    assert not any(call.args and call.args[0] == "access_granted_once" for call in _published(mock_publish)), (
        "'access_granted_once' belonged to the gift kind and has no publisher left; the bot branch "
        "that consumed it is gone with it"
    )
    assert not any(call.args and call.args[0] == "access_offered" for call in _published(mock_publish))


def test_grant_free_provisions_immediately(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):

    tariff = two_inbounds_and_tariff
    db.session.add(TelegramUser(telegram_id=42, language="en"))
    db.session.commit()

    with (
        patch("panel_core.services.runtime_apply.generate_config_file"),
        patch("panel_core.services.bot_events.publish_stored") as mock_publish,
    ):
        resp = client.post(
            "/api/bot/users/42/grants",
            headers=admin_headers,
            json={"tariff_id": tariff.id, "billing": "free", "note": "VIP"},
        )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["billing"] == "free"
    assert body["access_until"] is None, f"a grant issued with no term is open-ended; got {body!r}"
    assert body["next_renewal_at"] is not None, (
        "this tariff limits traffic on one of its inbounds, so the counter still has to be zeroed "
        f"once a period; got {body!r}"
    )
    assert Client.query.filter_by(telegram_id=42).count() == 2

    grant_calls = [c for c in _published(mock_publish) if c.args and c.args[0] == "access_granted"]
    assert len(grant_calls) == 1
    event_type, tg_id, payload = grant_calls[0].args
    assert tg_id == 42
    assert payload["tariff_name"] == tariff.name
    assert payload["lang"] == "en"
    assert payload["expires_at_ms"] == 0, (
        "0 is what every layer reads as 'never', and it is what the bot renders as permanent -- a "
        f"date here would promise the holder an end that no longer exists; got {payload!r}"
    )
    assert not any(call.args and call.args[0] == "access_offered" for call in _published(mock_publish))


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

    tariff = two_inbounds_and_tariff
    db.session.add(UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="paid"))
    db.session.commit()

    with patch("panel_core.services.runtime_apply.generate_config_file"):
        resp = client.post(
            "/api/bot/users/42/grants",
            headers=admin_headers,
            json={"tariff_id": tariff.id, "billing": "free"},
        )
    assert resp.status_code == 201
    rows = UserTariffAccess.query.filter_by(telegram_id=42).all()
    assert len(rows) == 1
    assert rows[0].billing == "free"


def test_list_grants(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):

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


def _make_user_with_active_clients(db, tariff, telegram_id=42):

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


def _operation_request(client, headers, operation, tariff_id):
    if operation == "revoke":
        return client.delete(f"/api/bot/users/42/tariffs/{tariff_id}", headers=headers)
    return client.post(f"/api/bot/users/42/{operation}", headers=headers)


def test_revoke_keeps_tombstone_and_counts_actual_clients(
    app_with_admin, db, client, admin_headers, two_inbounds_and_tariff
):
    tariff = two_inbounds_and_tariff
    clients = _make_user_with_active_clients(db, tariff)
    grant = UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="free")
    db.session.add(grant)
    db.session.commit()
    response = _operation_request(client, admin_headers, "revoke", tariff.id)
    body = response.get_json()
    assert response.status_code == 200, body
    assert body["disabled_clients"] == 2
    assert body["remote_disabled"] == 0
    assert body["revoked_grants"] == 1
    assert body["panel_failures"] == []
    assert all(not row.enable and row.expiry_time < int(time.time() * 1000) for row in clients)
    assert db.session.get(UserTariffAccess, grant.id).provisioning_status == "revoked"


def test_revoke_empty_is_idempotent(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()
    for _ in range(2):
        response = _operation_request(client, admin_headers, "revoke", two_inbounds_and_tariff.id)
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["disabled_clients"] == 0
        assert response.get_json()["revoked_grants"] == 0


@pytest.mark.parametrize("operation", ["block", "unblock", "revoke"])
@pytest.mark.parametrize("protocol", ["vless", "trojan"])
def test_access_mutation_applies_committed_runtime(
    app_with_admin, db, client, admin_headers, two_inbounds_and_tariff, runtime_io, operation, protocol, monkeypatch
):
    tariff = two_inbounds_and_tariff
    clients = _make_user_with_active_clients(db, tariff)
    for inbound in Inbound.query.all():
        inbound.protocol = protocol
    if operation == "unblock":
        db.session.get(TelegramUser, 42).blocked = True
        for row in clients:
            row.enable = False
    db.session.commit()
    order = []
    observed = []

    def apply_runtime(*args):
        order.append("runtime")
        observed.append([row.enable for row in Client.query.filter_by(telegram_id=42).all()])
        return True

    runtime_io[1].side_effect = apply_runtime
    monkeypatch.setattr("panel_core.services.entitlements._api_add_user_grpc", apply_runtime)
    monkeypatch.setattr("panel_core.services.entitlements._api_remove_user_grpc", apply_runtime)
    with _SqlOrderRecorder(order):
        response = _operation_request(client, admin_headers, operation, tariff.id)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["re_enabled" if operation == "unblock" else "disabled_clients"] == 2
    assert observed and observed[-1] == [operation == "unblock"] * 2
    for index, value in enumerate(order):
        if value == "runtime":
            boundary = max((i for i in range(index) if order[i] == "commit"), default=-1)
            assert "sql_write" not in order[boundary + 1 : index], order


@pytest.mark.parametrize("operation", ["block", "unblock", "revoke"])
def test_runtime_failure_is_pending_then_retry_recovers(
    app_with_admin, db, client, admin_headers, two_inbounds_and_tariff, runtime_io, operation, monkeypatch
):
    tariff = two_inbounds_and_tariff
    clients = _make_user_with_active_clients(db, tariff)
    if operation == "unblock":
        db.session.get(TelegramUser, 42).blocked = True
        for row in clients:
            row.enable = False
    db.session.commit()
    monkeypatch.setattr("panel_core.services.entitlements._api_add_user_grpc", lambda *args: False)
    monkeypatch.setattr("panel_core.services.entitlements._api_remove_user_grpc", lambda *args: False)
    runtime_io[1].side_effect = RuntimeError("runtime offline")
    response = _operation_request(client, admin_headers, operation, tariff.id)
    assert response.status_code == 202, response.get_json()
    assert response.get_json()["panel_failures"]
    operation_id = response.get_json()["operation_id"]
    assert db.session.get(ProvisionOperation, operation_id).status == "pending"
    runtime_io[1].side_effect = None
    from panel_core.services.provisioning_operations import run_operation

    result = run_operation(db.session.get(ProvisionOperation, operation_id))
    assert not result["panel_failures"], result
    assert db.session.get(ProvisionOperation, operation_id).status == "succeeded"
    assert all(row.enable == (operation == "unblock") for row in Client.query.filter_by(telegram_id=42))


def test_block_preserves_grants_for_later_unblock(app_with_admin, db, client, admin_headers, two_inbounds_and_tariff):
    tariff = two_inbounds_and_tariff
    _make_user_with_active_clients(db, tariff)
    grant = UserTariffAccess(telegram_id=42, tariff_id=tariff.id, billing="free")
    db.session.add(grant)
    db.session.commit()
    response = _operation_request(client, admin_headers, "block", tariff.id)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["disabled_clients"] == 2
    assert response.get_json()["cancelled_grants"] == 0
    assert db.session.get(UserTariffAccess, grant.id) is not None
    response = _operation_request(client, admin_headers, "unblock", tariff.id)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["re_enabled"] == 2


@pytest.mark.parametrize(
    "expiry,limit,used,manual,expected",
    [
        (0, 0, 0, False, True),
        (4102444800000, 100, 20, False, True),
        (1, 0, 0, False, False),
        (0, 100, 100, False, False),
        (0, 0, 0, True, False),
    ],
)
def test_unblock_honors_expiry_quota_and_manual_override(
    app_with_admin, db, client, admin_headers, two_inbounds_and_tariff, expiry, limit, used, manual, expected
):
    clients = _make_user_with_active_clients(db, two_inbounds_and_tariff)
    db.session.delete(clients[1])
    row = clients[0]
    row.enable, row.expiry_time, row.limit_bytes, row.up, row.manual_disabled = False, expiry, limit, used, manual
    db.session.get(TelegramUser, 42).blocked = True
    db.session.commit()
    response = _operation_request(client, admin_headers, "unblock", two_inbounds_and_tariff.id)
    assert response.status_code == 200, response.get_json()
    assert row.enable is expected
    assert response.get_json()["re_enabled"] == int(expected)
    assert db.session.get(TelegramUser, 42).blocked is False


@pytest.mark.parametrize("operation", ["block", "unblock"])
def test_account_without_clients_never_touches_runtime(
    app_with_admin, db, client, admin_headers, runtime_io, operation
):
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()
    response = _operation_request(client, admin_headers, operation, 0)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["disabled_clients"] == 0
    assert response.get_json()["re_enabled"] == 0
    runtime_io[0].assert_not_called()
    runtime_io[1].assert_not_called()


@pytest.mark.parametrize("operation", ["block", "unblock"])
@pytest.mark.parametrize("failed_panels", [[], [1, 2], [2]])
def test_remote_account_results_count_only_successful_nodes(
    app_with_admin, db, client, admin_headers, two_inbounds_and_tariff, operation, failed_panels
):
    clients = _make_user_with_active_clients(db, two_inbounds_and_tariff)
    if operation == "unblock":
        db.session.get(TelegramUser, 42).blocked = True
        for row in clients:
            row.enable = False
    for pid in (1, 2):
        _add_linked_panel(db, pid=pid, name=f"node{pid}")
    db.session.commit()
    calls = []

    def remote(connection, verb, path, **kwargs):
        pid = 1 if "node1." in connection.base_url else 2
        calls.append((pid, path, kwargs["json"]))
        if pid in failed_panels:
            raise ValueError("panel offline")
        return {
            "revision": kwargs["json"]["revision"],
            "blocked": operation == "block",
            "disabled_clients": 2 if operation == "block" else 0,
            "re_enabled": 3 if operation == "unblock" else 0,
            "affected_clients": 3,
        }

    with patch("panel_core.services.panel_proxy.FederationClient._call_reporting", autospec=True, side_effect=remote):
        response = _operation_request(client, admin_headers, operation, two_inbounds_and_tariff.id)
    body = response.get_json()
    assert response.status_code == (202 if failed_panels else 200), body
    assert body["re_enabled" if operation == "unblock" else "disabled_clients"] == 2
    assert body["remote_re_enabled" if operation == "unblock" else "remote_disabled"] == (2 - len(failed_panels)) * (
        3 if operation == "unblock" else 2
    )
    assert sorted(failure["panel_id"] for failure in body["panel_failures"]) == failed_panels
    assert all(
        path == "/api/federation/account-access"
        and payload["telegram_id"] == 42
        and payload["blocked"] == (operation == "block")
        for _, path, payload in calls
    )
    assert db.session.get(TelegramUser, 42).blocked is (operation == "block")


@pytest.mark.parametrize("failed_panels", [[], [1, 2], [2]])
def test_remote_revoke_is_source_scoped_and_journaled(
    app_with_admin, db, client, admin_headers, two_inbounds_and_tariff, failed_panels
):
    tariff = two_inbounds_and_tariff
    _make_user_with_active_clients(db, tariff)
    for pid in (1, 2):
        _add_linked_panel(db, pid=pid, name=f"node{pid}")
        db.session.add(TariffItem(tariff_id=tariff.id, panel_id=pid, inbound_tag=f"remote{pid}", traffic_gb=1))
    _add_linked_panel(db, pid=3, name="unrelated")
    db.session.commit()
    calls = []

    def remote(connection, verb, path, **kwargs):
        pid = 1 if "node1." in connection.base_url else 2
        calls.append((pid, path, kwargs["json"]))
        assert "unrelated" not in connection.base_url
        if pid in failed_panels:
            raise ValueError("panel offline")
        return {"disabled_clients": 1, "expires_at_ms": None}

    with patch("panel_core.services.panel_proxy.FederationClient._call_reporting", autospec=True, side_effect=remote):
        response = _operation_request(client, admin_headers, "revoke", tariff.id)
    body = response.get_json()
    assert response.status_code == (202 if failed_panels else 200), body
    assert body["disabled_clients"] == 2
    assert body["remote_disabled"] == 2 - len(failed_panels)
    assert sorted(failure["panel_id"] for failure in body["panel_failures"]) == failed_panels
    assert all(
        path == "/api/federation/entitlements/revoke"
        and payload["tariff_id"] == tariff.id
        and payload["source_id"] == f"tariff:42:{tariff.id}"
        for _, path, payload in calls
    )
    record = db.session.get(ProvisionOperation, body["operation_id"])
    assert record.status == ("pending" if failed_panels else "succeeded")
    assert len(record.target_states) == 4


def _add_linked_panel(db, *, pid, name, enable=True):
    db.session.add(
        LinkedPanel(
            id=pid,
            name=name,
            url=f"https://{name}.example.com",
            federation_token="tok",
            enable=enable,
            created_at=0,
        )
    )


def test_live_enumeration_buckets_clients_and_reports_unreachable(app_with_admin, db):

    from panel_core.services.remote_clients import (
        remote_clients_by_telegram_id_live as _remote_clients_by_telegram_id_live,
    )

    _add_linked_panel(db, pid=1, name="Child-A")
    _add_linked_panel(db, pid=2, name="Child-B")
    db.session.commit()

    snap_a = {
        "inbounds": [
            {"tag": "FR", "label": "France", "clients": [{"telegram_id": 42, "email": "tg42_FR", "enable": True}]}
        ]
    }

    def _fetch(panel_id):
        if panel_id == 1:
            return snap_a
        raise ValueError("panel offline")

    with patch("panel_core.services.remote_clients.fetch_panel_snapshot_live", side_effect=_fetch):
        bucket, unreachable = _remote_clients_by_telegram_id_live()

    assert list(bucket.keys()) == [42]
    assert bucket[42][0]["email"] == "tg42_FR"
    assert bucket[42][0]["panel_id"] == 1
    assert unreachable == [{"panel_id": 2, "panel_name": "Child-B", "error": "panel offline"}]


def test_live_enumeration_scopes_to_given_panel_ids(app_with_admin, db):

    from panel_core.services.remote_clients import (
        remote_clients_by_telegram_id_live as _remote_clients_by_telegram_id_live,
    )

    _add_linked_panel(db, pid=1, name="Child-A")
    _add_linked_panel(db, pid=2, name="Child-B")
    db.session.commit()

    fetched: list[int] = []

    def _fetch(panel_id):
        fetched.append(panel_id)
        return {"inbounds": []}

    with patch("panel_core.services.remote_clients.fetch_panel_snapshot_live", side_effect=_fetch):
        bucket, unreachable = _remote_clients_by_telegram_id_live(panel_ids={1})

    assert fetched == [1]
    assert bucket == {}
    assert unreachable == []


def test_warning_history_counts_only_delivered_messages_and_separates_users(client, admin_headers, db):
    from datetime import datetime, timedelta, timezone
    from panel_core.models import BotDelivery

    tg_id = 8_000_000_042
    db.session.add(TelegramUser(telegram_id=tg_id, language="ru"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    records = [
        ("traffic_notification", "traffic_80", "delivered", tg_id, now),
        ("expiry_notification", "expiry_1d", "delivered", tg_id, now),
        ("expiry_notification", "expiry_1d", "suppressed", tg_id, now),
        ("traffic_notification", "traffic_95", "pending", tg_id, now),
        ("expiry_notification", "expiry_1h", "permanent", tg_id, now),
        ("payment_succeeded", "", "delivered", tg_id, now),
        ("traffic_notification", "traffic_80", "delivered", 99, now),
        ("expiry_notification", "expiry_3d", "delivered", tg_id, now - timedelta(days=91)),
    ]
    for i, (event_type, kind, state, recipient, when) in enumerate(records):
        db.session.add(
            BotDelivery(
                source="node:test",
                event_id=i + 1,
                state=state,
                created_at=when,
                updated_at=when,
                event={
                    "type": event_type,
                    "telegram_id": recipient,
                    "payload": {"kind": kind, "node": "node.example", "inbound_tag": "vpn"},
                },
            )
        )
    db.session.commit()
    result = client.get(f"/api/bot/users/{tg_id}/warnings?limit=2", headers=admin_headers)
    assert result.status_code == 200
    data = result.json
    assert data["total"] == 5
    assert data["sent"] == 2
    assert data["traffic_sent"] == data["expiry_sent"] == 1
    assert data["pending"] == data["failed"] == data["suppressed"] == 1
    assert data["days"] == 90
    assert [item["kind"] for item in data["items"]] == ["expiry_1h", "traffic_95"]
    assert all(item["updated_at"].endswith("Z") for item in data["items"])
    second = client.get(f"/api/bot/users/{tg_id}/warnings?limit=2&offset=2", headers=admin_headers).json
    assert second["items"][0]["state"] == "suppressed"
    assert second["sent"] == 2
    assert second["items"][0]["node"] == "node.example"


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=no", "offset=-1"])
def test_warning_history_rejects_invalid_pagination(client, admin_headers, db, query):
    db.session.add(TelegramUser(telegram_id=42, language="ru"))
    db.session.commit()
    assert client.get("/api/bot/users/42/warnings?" + query, headers=admin_headers).status_code == 400


def test_warning_history_requires_admin_and_existing_user(client, admin_headers):
    assert client.get("/api/bot/users/42/warnings").status_code == 401
    assert client.get("/api/bot/users/42/warnings", headers=admin_headers).status_code == 404
