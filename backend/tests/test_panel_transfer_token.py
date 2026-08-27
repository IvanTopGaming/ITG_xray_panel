import base64
import time
from unittest.mock import patch

import jwt as jwt_lib
import pytest

from panel_core.models import Admin, LinkedPanel
from panel_core.utils import SECRET_KEY


def _panel(db):
    panel = LinkedPanel(name="alpha", url="https://alpha.example.com/secret", federation_token="old-fed", created_at=1)
    db.session.add(panel)
    db.session.commit()
    return panel


@pytest.fixture
def app(app):

    from panel_core.api import panels

    if not any(bp_name == "panels" for bp_name in app.blueprints):
        app.register_blueprint(panels.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_token(app, db):
    pwd_version = int(time.time())
    admin = Admin(
        username="admin",
        password="hashed-not-checked-by-token-required",
        password_changed_at=pwd_version,
    )
    db.session.add(admin)
    db.session.commit()
    return jwt_lib.encode(
        {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": pwd_version,
            "exp": int(time.time()) + 3600,
        },
        SECRET_KEY,
        algorithm="HS256",
    )


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_issuing_a_token_tries_a_live_copy_first(app, db):
    from panel_core.services import panel_transfer

    panel = _panel(db)
    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.return_value = {
            "hot": {"inbounds": []},
            "cold": {"outbounds": []},
            "fingerprint": "a" * 64,
            "instance_id": "inst-1",
            "app_version": "3.2.0",
        }
        result = panel_transfer.issue_transfer_token(panel, carry_admin=True)

    assert result["state_freshness"]["ok"] is True, (
        "в сценарии планового переезда нода ещё жива — копия снимается прямо перед выдачей ключа, "
        "и зеркало в переносе вообще не участвует"
    )
    assert client_cls.return_value.state.called


def test_an_unreachable_node_still_issues_a_token_but_says_how_stale(app, db):
    from panel_core.services import panel_transfer
    from panel_core.services.state_mirror import write_hot

    panel = _panel(db)
    write_hot(
        panel.id,
        {"inbounds": []},
        taken_at=1_700_000_000_000,
        instance_id="i",
        app_version="3.2.0",
        shrink_flagged=False,
    )

    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.side_effect = RuntimeError("unreachable")
        result = panel_transfer.issue_transfer_token(panel, carry_admin=True)

    assert result["token"]
    assert result["state_freshness"]["ok"] is False
    assert result["state_freshness"]["taken_at"] == 1_700_000_000_000, (
        "цену переноса надо показать до того, как админ нажал, а не после"
    )


def test_the_token_carries_the_master_url(app, db, monkeypatch):
    from panel_core.services import panel_transfer

    monkeypatch.setenv("PANEL_DOMAIN", "hq.example.com")
    monkeypatch.setenv("PANEL_SECRET_PATH", "master-secret")
    panel = _panel(db)

    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.side_effect = RuntimeError("nope")
        token = panel_transfer.issue_transfer_token(panel, carry_admin=False)["token"]

    decoded = base64.urlsafe_b64decode(token + "==").decode()
    url, _, secret = decoded.partition("|")
    assert url == "https://hq.example.com/master-secret"
    assert len(secret) >= 32


def test_a_second_issue_invalidates_the_first(app, db):
    from panel_core.services import panel_transfer

    panel = _panel(db)
    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.side_effect = RuntimeError("nope")
        first = panel_transfer.issue_transfer_token(panel, carry_admin=False)["token"]
        second = panel_transfer.issue_transfer_token(panel, carry_admin=False)["token"]

    assert first != second
    assert panel.transfer_token_used is False
    assert panel.transfer_claimed_instance_id is None


def test_carry_admin_is_recorded_on_the_panel(app, db):
    from panel_core.services import panel_transfer

    panel = _panel(db)
    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.side_effect = RuntimeError("nope")
        panel_transfer.issue_transfer_token(panel, carry_admin=False)

    assert panel.transfer_carry_admin is False


def test_endpoint_requires_an_admin_jwt(app, db):
    panel = _panel(db)
    assert app.test_client().post(f"/api/panels/{panel.id}/transfer-token").status_code == 401


def test_endpoint_rejects_a_federation_token(client, db):
    panel = _panel(db)
    resp = client.post(
        f"/api/panels/{panel.id}/transfer-token",
        headers={"X-Federation-Token": panel.federation_token},
    )
    assert resp.status_code == 401


def test_a_malformed_reply_does_not_crash_the_endpoint(app, db):
    from panel_core.services import panel_transfer

    panel = _panel(db)
    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.return_value = ["not", "a", "dict"]
        result = panel_transfer.issue_transfer_token(panel, carry_admin=True)

    assert result["token"], "ключ выдаётся даже если ответ ноды не разобрать"
    assert result["state_freshness"]["ok"] is False


def test_a_mirror_write_failure_does_not_crash_the_endpoint(app, db):
    from panel_core.services import panel_transfer

    panel = _panel(db)
    with (
        patch.object(panel_transfer, "FederationClient") as client_cls,
        patch.object(panel_transfer, "write_hot", side_effect=RuntimeError("db is down")),
    ):
        client_cls.return_value.state.return_value = {
            "hot": {"inbounds": []},
            "cold": {"outbounds": []},
            "fingerprint": "b" * 64,
            "instance_id": "inst-2",
            "app_version": "3.2.0",
        }
        result = panel_transfer.issue_transfer_token(panel, carry_admin=True)

    assert result["token"], "падение записи зеркала не должно ронять выдачу ключа"
    assert result["state_freshness"]["ok"] is False


def test_inbounds_not_a_list_leaves_the_mirror_untouched(app, db):
    from panel_core.services import panel_transfer
    from panel_core.services.state_mirror import read_current, write_hot

    panel = _panel(db)
    write_hot(
        panel.id,
        {"inbounds": []},
        taken_at=1_700_000_000_000,
        instance_id="i",
        app_version="3.2.0",
        shrink_flagged=False,
    )

    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.return_value = {
            "hot": {"inbounds": "oops-not-a-list"},
            "cold": {},
            "fingerprint": "c" * 64,
            "instance_id": "inst-3",
            "app_version": "3.2.0",
        }
        result = panel_transfer.refresh_mirror_live(panel)

    assert result["ok"] is False
    assert result["taken_at"] == 1_700_000_000_000
    row = read_current(panel.id)
    assert row.hot_updated_at == 1_700_000_000_000, "невалидный по форме ответ не должен переписывать зеркало"


def test_a_shrunk_reply_still_raises_the_shrink_flag(app, db):
    from panel_core.services import panel_transfer
    from panel_core.services.state_mirror import read_current, write_hot

    panel = _panel(db)
    write_hot(
        panel.id,
        {"inbounds": [{"clients": [{"email": f"u{i}"} for i in range(10)]}]},
        taken_at=1_700_000_000_000,
        instance_id="i",
        app_version="3.2.0",
        shrink_flagged=False,
    )

    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.return_value = {
            "hot": {"inbounds": [{"clients": [{"email": "u0"}]}]},
            "cold": {},
            "fingerprint": "d" * 64,
            "instance_id": "inst-4",
            "app_version": "3.2.0",
        }
        result = panel_transfer.refresh_mirror_live(panel)

    assert result["ok"] is True
    row = read_current(panel.id)
    assert row.shrink_flagged is True, (
        "выдача ключа не должна гасить флаг обвала числа клиентов, который увидел бы опрос"
    )


def test_empty_panel_domain_refuses_to_issue_a_broken_key(app, db, monkeypatch):
    from panel_core.services import panel_transfer

    monkeypatch.setenv("PANEL_DOMAIN", "")
    panel = _panel(db)

    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.side_effect = RuntimeError("should not be called")
        with pytest.raises(ValueError):
            panel_transfer.issue_transfer_token(panel, carry_admin=True)

    assert panel.transfer_token is None, "падение на адресе мастера не должно гасить уже выданный ключ"


def test_empty_panel_secret_path_refuses_to_issue_a_broken_key(app, db, monkeypatch):
    from panel_core.services import panel_transfer

    monkeypatch.setenv("PANEL_SECRET_PATH", "")
    panel = _panel(db)

    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.side_effect = RuntimeError("should not be called")
        with pytest.raises(ValueError):
            panel_transfer.issue_transfer_token(panel, carry_admin=True)

    assert panel.transfer_token is None


def test_endpoint_answers_400_when_the_master_url_is_unconfigured(client, db, admin_token, monkeypatch):
    from panel_core.services import panel_transfer

    monkeypatch.setenv("PANEL_SECRET_PATH", "")
    panel = _panel(db)

    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.side_effect = RuntimeError("should not be called")
        resp = client.post(f"/api/panels/{panel.id}/transfer-token", headers=_auth(admin_token))
    assert resp.status_code == 400


def test_endpoint_issues_a_token_for_an_authenticated_admin(client, db, admin_token):
    from panel_core.services import panel_transfer

    panel = _panel(db)
    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.side_effect = RuntimeError("nope")
        resp = client.post(f"/api/panels/{panel.id}/transfer-token", headers=_auth(admin_token))

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["token"]
    decoded = base64.urlsafe_b64decode(body["token"] + "==").decode()
    assert "|" in decoded
    assert body["state_freshness"]["ok"] is False


def test_default_carry_admin_is_true_over_the_endpoint(client, db, admin_token):
    from panel_core.services import panel_transfer

    panel = _panel(db)
    with patch.object(panel_transfer, "FederationClient") as client_cls:
        client_cls.return_value.state.side_effect = RuntimeError("nope")
        resp = client.post(f"/api/panels/{panel.id}/transfer-token", headers=_auth(admin_token))

    assert resp.status_code == 200
    fresh = db.session.get(LinkedPanel, panel.id)
    assert fresh.transfer_carry_admin is True, (
        "carry_admin по умолчанию решает, переезжает ли хеш пароля админа ноды — молчаливый запрос "
        "без тела обязан переносить его"
    )


def test_endpoint_404s_for_an_unknown_panel(client, db, admin_token):
    resp = client.post("/api/panels/999999/transfer-token", headers=_auth(admin_token))
    assert resp.status_code == 404
