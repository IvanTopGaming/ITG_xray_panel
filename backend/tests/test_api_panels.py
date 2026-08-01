import time
from unittest.mock import patch, MagicMock

import jwt as jwt_lib
import pytest

from panel_core.models import Admin, LinkedPanel, SystemSetting, Tariff, TariffItem
from panel_core.utils import SECRET_KEY


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:5000",
        "http://localhost/api",
        "http://10.1/api",
        "http://127.1/api",
        "http://0177.0.0.1/api",
        "http://169.254.169.254/latest",
        "http://socket-proxy:2375",
        "http://redis:6379",
        "https://panel.local/api",
        "ftp://example.com",
    ],
)
def test_validate_panel_url_rejects_internal(bad_url):
    from panel_core.api.panels import _validate_panel_url

    with pytest.raises(ValueError):
        _validate_panel_url(bad_url)


@pytest.mark.parametrize(
    "ok_url",
    ["https://child.example.com", "https://panel.acme.io:8443/x", "http://8.8.8.8/api"],
)
def test_validate_panel_url_allows_public(ok_url):
    from panel_core.api.panels import _validate_panel_url

    assert _validate_panel_url(ok_url) == ok_url


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
    token = jwt_lib.encode(
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
    return token


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _make_panel(db, name="child-1", url="https://child.example.com", token="fed-tok-123"):
    now = int(time.time())
    panel = LinkedPanel(
        name=name,
        url=url,
        federation_token=token,
        created_at=now,
    )
    db.session.add(panel)
    db.session.commit()
    return panel


def test_list_panels_empty(client, admin_token):
    resp = client.get("/api/panels", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_list_panels_returns_ordered(client, admin_token, db):
    _make_panel(db, name="b-panel", url="https://b.example.com")
    _make_panel(db, name="a-panel", url="https://a.example.com")
    resp = client.get("/api/panels", headers=_auth(admin_token))
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 2

    assert data[0]["name"] == "b-panel"
    assert data[1]["name"] == "a-panel"


def test_list_panels_masks_token(client, admin_token, db):
    _make_panel(db, name="secret-panel", token="super-secret")
    resp = client.get("/api/panels", headers=_auth(admin_token))
    data = resp.get_json()
    assert data[0]["federation_token"] == "••••••••"


def test_list_panels_requires_auth(client):
    resp = client.get("/api/panels")
    assert resp.status_code == 401


def test_list_panels_overlays_live_status_from_redis(client, admin_token, db):
    panel = _make_panel(db)
    panel.status = "online"
    panel.last_poll = 111
    db.session.commit()

    fake = MagicMock()
    fake.get.side_effect = lambda key: {
        f"panel:{panel.id}:status": b"offline",
        f"panel:{panel.id}:last_poll": b"1781200000000",
    }.get(key)

    with patch("panel_core.services.panel_proxy.get_shared_redis", return_value=fake):
        resp = client.get("/api/panels", headers=_auth(admin_token))

    assert resp.status_code == 200
    item = next(p for p in resp.get_json() if p["id"] == panel.id)
    assert item["status"] == "offline"
    assert item["last_poll"] == 1781200000000


def test_list_panels_keeps_db_values_when_redis_empty(client, admin_token, db):
    panel = _make_panel(db)
    panel.status = "online"
    panel.last_poll = 111
    db.session.commit()

    fake = MagicMock()
    fake.get.return_value = None

    with patch("panel_core.services.panel_proxy.get_shared_redis", return_value=fake):
        resp = client.get("/api/panels", headers=_auth(admin_token))

    item = next(p for p in resp.get_json() if p["id"] == panel.id)
    assert item["status"] == "online"
    assert item["last_poll"] == 111


@patch("panel_core.api.panels.requests.post")
def test_create_panel_success(mock_post, client, admin_token, db):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"federation_token": "new-fed-token-xyz"}
    mock_post.return_value = mock_resp

    resp = client.post(
        "/api/panels",
        headers=_auth(admin_token),
        json={"name": "new-child", "url": "https://child.example.com", "link_token": "link-abc"},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["name"] == "new-child"
    assert data["url"] == "https://child.example.com"
    assert data["federation_token"] == "••••••••"

    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args[0][0] == "https://child.example.com/api/federation/handshake"
    assert call_args[1]["json"]["link_token"] == "link-abc"
    assert call_args[1]["json"]["master_name"] == "Master"
    assert call_args[1]["timeout"] == 10

    panel = LinkedPanel.query.filter_by(name="new-child").first()
    assert panel is not None
    assert panel.federation_token == "new-fed-token-xyz"


@patch("panel_core.api.panels.requests.post")
def test_create_panel_uses_custom_master_name(mock_post, client, admin_token, db):
    db.session.add(SystemSetting(key="panel_name", value="My Master"))
    db.session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"federation_token": "tok"}
    mock_post.return_value = mock_resp

    resp = client.post(
        "/api/panels",
        headers=_auth(admin_token),
        json={"name": "child", "url": "https://c.example.com", "link_token": "lt"},
    )
    assert resp.status_code == 201
    call_json = mock_post.call_args[1]["json"]
    assert call_json["master_name"] == "My Master"


def test_create_panel_missing_name(client, admin_token):
    resp = client.post(
        "/api/panels",
        headers=_auth(admin_token),
        json={"url": "https://x.com", "link_token": "lt"},
    )
    assert resp.status_code == 400
    assert "name" in resp.get_json()["error"].lower()


def test_create_panel_missing_url(client, admin_token):
    resp = client.post(
        "/api/panels",
        headers=_auth(admin_token),
        json={"name": "p", "link_token": "lt"},
    )
    assert resp.status_code == 400
    assert "url" in resp.get_json()["error"].lower()


@patch("panel_core.api.panels.requests.post")
def test_create_panel_duplicate_name(mock_post, client, admin_token, db):
    _make_panel(db, name="taken")
    resp = client.post(
        "/api/panels",
        headers=_auth(admin_token),
        json={"name": "taken", "url": "https://x.com", "link_token": "lt"},
    )
    assert resp.status_code == 400
    assert "already exists" in resp.get_json()["error"]


@patch("panel_core.api.panels.requests.post")
def test_create_panel_connection_error(mock_post, client, admin_token):
    mock_post.side_effect = __import__("requests").ConnectionError("refused")
    resp = client.post(
        "/api/panels",
        headers=_auth(admin_token),
        json={"name": "unreachable", "url": "https://down.example.com", "link_token": "lt"},
    )
    assert resp.status_code == 502
    assert "connect" in resp.get_json()["error"].lower()


@patch("panel_core.api.panels.requests.post")
def test_create_panel_timeout(mock_post, client, admin_token):
    mock_post.side_effect = __import__("requests").Timeout("timed out")
    resp = client.post(
        "/api/panels",
        headers=_auth(admin_token),
        json={"name": "slow", "url": "https://slow.example.com", "link_token": "lt"},
    )
    assert resp.status_code == 502
    assert "timed out" in resp.get_json()["error"].lower()


@patch("panel_core.api.panels.requests.post")
def test_create_panel_handshake_rejected(mock_post, client, admin_token):
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.json.return_value = {"error": "invalid link token"}
    mock_resp.text = "invalid link token"
    mock_post.return_value = mock_resp

    resp = client.post(
        "/api/panels",
        headers=_auth(admin_token),
        json={"name": "rejected", "url": "https://child.example.com", "link_token": "bad"},
    )
    assert resp.status_code == 502
    assert "invalid link token" in resp.get_json()["error"].lower()


def test_update_panel_name(client, admin_token, db):
    panel = _make_panel(db, name="old-name")
    resp = client.put(
        f"/api/panels/{panel.id}",
        headers=_auth(admin_token),
        json={"name": "new-name"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "new-name"


def test_update_panel_enable(client, admin_token, db):
    panel = _make_panel(db, name="toggle-me")
    assert panel.enable is True
    resp = client.put(
        f"/api/panels/{panel.id}",
        headers=_auth(admin_token),
        json={"enable": False},
    )
    assert resp.status_code == 200
    assert resp.get_json()["enable"] is False


def test_update_panel_duplicate_name(client, admin_token, db):
    _make_panel(db, name="existing")
    other = _make_panel(db, name="will-rename", url="https://other.example.com")
    resp = client.put(
        f"/api/panels/{other.id}",
        headers=_auth(admin_token),
        json={"name": "existing"},
    )
    assert resp.status_code == 400
    assert "already exists" in resp.get_json()["error"]


def test_update_panel_same_name_ok(client, admin_token, db):
    panel = _make_panel(db, name="keep-me")
    resp = client.put(
        f"/api/panels/{panel.id}",
        headers=_auth(admin_token),
        json={"name": "keep-me"},
    )
    assert resp.status_code == 200


def test_update_panel_not_found(client, admin_token):
    resp = client.put(
        "/api/panels/9999",
        headers=_auth(admin_token),
        json={"name": "ghost"},
    )
    assert resp.status_code == 404


def test_delete_panel(client, admin_token, db):
    panel = _make_panel(db, name="doomed")
    panel_id = panel.id
    resp = client.delete(f"/api/panels/{panel_id}", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert db.session.get(LinkedPanel, panel_id) is None


@patch("panel_core.services.panel_proxy.get_shared_redis")
def test_delete_panel_cleans_redis(mock_get_redis, client, admin_token, db):
    mock_redis = MagicMock()
    mock_get_redis.return_value = mock_redis
    panel = _make_panel(db, name="cached")
    panel_id = panel.id

    resp = client.delete(f"/api/panels/{panel_id}", headers=_auth(admin_token))
    assert resp.status_code == 200
    mock_redis.delete.assert_called_once_with(
        f"panel:{panel_id}:snapshot",
        f"panel:{panel_id}:status",
        f"panel:{panel_id}:last_poll",
        f"panel:{panel_id}:snapshot:last",
        f"panel:{panel_id}:last_poll:last",
    )


def test_delete_panel_not_found(client, admin_token):
    resp = client.delete("/api/panels/9999", headers=_auth(admin_token))
    assert resp.status_code == 404


def _make_tariff(db, name="T", enabled=True, is_trial=False):
    tariff = Tariff(name=name, price_rub=100, period_days=30, enabled=enabled, is_trial=is_trial)
    db.session.add(tariff)
    db.session.commit()
    return tariff


def _add_item(db, tariff_id, tag, panel_id=None, order=0):
    item = TariffItem(tariff_id=tariff_id, inbound_tag=tag, traffic_gb=0, panel_id=panel_id, sort_order=order)
    db.session.add(item)
    db.session.commit()
    return item


def test_delete_panel_purges_its_tariff_items_and_keeps_others(client, admin_token, db):
    panel = _make_panel(db, name="doomed")
    other = _make_panel(db, name="survivor", url="https://s.example.com")
    tariff = _make_tariff(db, name="Mixed")
    local_id = _add_item(db, tariff.id, "local-in", panel_id=None, order=0).id
    doomed_id = _add_item(db, tariff.id, "remote-in", panel_id=panel.id, order=1).id
    other_id = _add_item(db, tariff.id, "other-in", panel_id=other.id, order=2).id

    resp = client.delete(f"/api/panels/{panel.id}", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert resp.get_json()["removed_tariff_items"] == 1
    assert TariffItem.query.filter_by(id=doomed_id).count() == 0
    assert TariffItem.query.filter_by(id=local_id).count() == 1
    assert TariffItem.query.filter_by(id=other_id).count() == 1


def test_delete_panel_disables_emptied_tariff(client, admin_token, db):
    panel = _make_panel(db, name="solo")
    tariff = _make_tariff(db, name="OnlyRemote")
    _add_item(db, tariff.id, "remote-in", panel_id=panel.id)

    resp = client.delete(f"/api/panels/{panel.id}", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert tariff.id in resp.get_json()["disabled_tariffs"]
    assert db.session.get(Tariff, tariff.id).enabled is False


def _probe(monkeypatch, *, raises=None):
    """The probe goes through FederationClient now, not a second raw requests stack (§61)."""

    calls = []

    class Stub:
        def __init__(self, url, token):
            calls.append((url, token))

        def snapshot(self):
            if raises is not None:
                raise raises
            return {"inbounds": []}

    monkeypatch.setattr("panel_core.api.panels.FederationClient", Stub)
    return calls


def test_test_panel_online(client, admin_token, db, monkeypatch):
    panel = _make_panel(db, name="healthy")
    calls = _probe(monkeypatch)
    nudged = []
    monkeypatch.setattr("panel_core.api.panels._nudge_panel_refresh", lambda pid: nudged.append(pid))

    data = client.post(f"/api/panels/{panel.id}/test", headers=_auth(admin_token)).get_json()

    assert data["status"] == "online"
    assert data["last_error"] is None
    assert isinstance(data["latency_ms"], int)
    assert calls == [("https://child.example.com", "fed-tok-123")]
    assert nudged == [panel.id], (
        "the probe must ask the cron host to re-poll rather than write the record itself — that is "
        "how an admin action refreshes a fact it does not own"
    )


def test_test_panel_reports_the_nodes_own_words(client, admin_token, db, monkeypatch):
    from panel_core.services.panel_proxy import RemotePanelError

    panel = _make_panel(db, name="errored")
    _probe(monkeypatch, raises=RemotePanelError(500, "Xray failed to start"))

    data = client.post(f"/api/panels/{panel.id}/test", headers=_auth(admin_token)).get_json()

    assert data["status"] == "error"
    assert data["last_error"] == "Xray failed to start", (
        "the admin is shown a generic message while the node said something specific — the whole of §61"
    )
    assert data["latency_ms"] is None


def test_test_panel_unreachable(client, admin_token, db, monkeypatch):
    from panel_core.services.panel_proxy import RemotePanelError

    panel = _make_panel(db, name="down")
    _probe(monkeypatch, raises=RemotePanelError(502, "Panel is unreachable: connection refused"))

    data = client.post(f"/api/panels/{panel.id}/test", headers=_auth(admin_token)).get_json()

    assert data["status"] == "offline"
    assert "unreachable" in data["last_error"]
    assert data["latency_ms"] is None


def test_the_probe_is_not_a_second_writer_of_the_panel_status(client, admin_token, db, monkeypatch):
    """Wave 2 gave `LinkedPanel.status` one owner, and this route quietly became a second.

    Worse than a duplicate: it produced a value (`"error"`) the cron host never writes, so the two
    did not even share a vocabulary and whichever ran last decided what the fleet looked like.
    """

    from panel_core.services.panel_proxy import RemotePanelError

    panel = _make_panel(db, name="stored")
    panel.status = "online"
    panel.last_poll = 111
    panel.last_error = None
    db.session.commit()
    stored_before = (panel.status, panel.last_poll, panel.last_error)
    _probe(monkeypatch, raises=RemotePanelError(502, "Panel is unreachable: refused"))
    monkeypatch.setattr("panel_core.api.panels._nudge_panel_refresh", lambda pid: None)

    body = client.post(f"/api/panels/{panel.id}/test", headers=_auth(admin_token)).get_json()
    assert body["status"] == "offline"

    fresh = db.session.get(LinkedPanel, panel.id)
    assert (fresh.status, fresh.last_poll, fresh.last_error) == stored_before, (
        "the probe wrote the stored record. It reports what it measured; the cron host owns what is written down."
    )
