import time
from unittest.mock import patch, MagicMock

import jwt as jwt_lib
import pytest

from app.models import Admin, LinkedPanel, SystemSetting
from app.utils import SECRET_KEY


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
    from app.api.panels import _validate_panel_url

    with pytest.raises(ValueError):
        _validate_panel_url(bad_url)


@pytest.mark.parametrize(
    "ok_url",
    ["https://child.example.com", "https://panel.acme.io:8443/x", "http://8.8.8.8/api"],
)
def test_validate_panel_url_allows_public(ok_url):
    from app.api.panels import _validate_panel_url

    assert _validate_panel_url(ok_url) == ok_url


@pytest.fixture
def app(app):

    from app.api import panels

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

    with patch("app.api.panels.get_redis", return_value=fake):
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

    with patch("app.api.panels.get_redis", return_value=fake):
        resp = client.get("/api/panels", headers=_auth(admin_token))

    item = next(p for p in resp.get_json() if p["id"] == panel.id)
    assert item["status"] == "online"
    assert item["last_poll"] == 111


@patch("app.api.panels.requests.post")
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


@patch("app.api.panels.requests.post")
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


@patch("app.api.panels.requests.post")
def test_create_panel_duplicate_name(mock_post, client, admin_token, db):
    _make_panel(db, name="taken")
    resp = client.post(
        "/api/panels",
        headers=_auth(admin_token),
        json={"name": "taken", "url": "https://x.com", "link_token": "lt"},
    )
    assert resp.status_code == 400
    assert "already exists" in resp.get_json()["error"]


@patch("app.api.panels.requests.post")
def test_create_panel_connection_error(mock_post, client, admin_token):
    mock_post.side_effect = __import__("requests").ConnectionError("refused")
    resp = client.post(
        "/api/panels",
        headers=_auth(admin_token),
        json={"name": "unreachable", "url": "https://down.example.com", "link_token": "lt"},
    )
    assert resp.status_code == 502
    assert "connect" in resp.get_json()["error"].lower()


@patch("app.api.panels.requests.post")
def test_create_panel_timeout(mock_post, client, admin_token):
    mock_post.side_effect = __import__("requests").Timeout("timed out")
    resp = client.post(
        "/api/panels",
        headers=_auth(admin_token),
        json={"name": "slow", "url": "https://slow.example.com", "link_token": "lt"},
    )
    assert resp.status_code == 502
    assert "timed out" in resp.get_json()["error"].lower()


@patch("app.api.panels.requests.post")
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


@patch("app.api.panels.get_redis")
def test_delete_panel_cleans_redis(mock_get_redis, client, admin_token, db):
    mock_redis = MagicMock()
    mock_get_redis.return_value = mock_redis
    panel = _make_panel(db, name="cached")
    panel_id = panel.id

    resp = client.delete(f"/api/panels/{panel_id}", headers=_auth(admin_token))
    assert resp.status_code == 200
    mock_redis.delete.assert_called_once_with(f"panel:{panel_id}:snapshot", f"panel:{panel_id}:status")


def test_delete_panel_not_found(client, admin_token):
    resp = client.delete("/api/panels/9999", headers=_auth(admin_token))
    assert resp.status_code == 404


@patch("app.api.panels.requests.get")
def test_test_panel_online(mock_get, client, admin_token, db):
    panel = _make_panel(db, name="healthy")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_get.return_value = mock_resp

    resp = client.post(f"/api/panels/{panel.id}/test", headers=_auth(admin_token))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "online"
    assert data["last_error"] is None
    assert "latency_ms" in data
    assert isinstance(data["latency_ms"], int)

    mock_get.assert_called_once()
    call_headers = mock_get.call_args[1]["headers"]
    assert call_headers["X-Federation-Token"] == "fed-tok-123"


@patch("app.api.panels.requests.get")
def test_test_panel_error_status(mock_get, client, admin_token, db):
    panel = _make_panel(db, name="errored")
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_get.return_value = mock_resp

    resp = client.post(f"/api/panels/{panel.id}/test", headers=_auth(admin_token))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "error"
    assert data["last_error"] == "HTTP 500"


@patch("app.api.panels.requests.get")
def test_test_panel_connection_refused(mock_get, client, admin_token, db):
    panel = _make_panel(db, name="down")
    mock_get.side_effect = __import__("requests").ConnectionError("refused")

    resp = client.post(f"/api/panels/{panel.id}/test", headers=_auth(admin_token))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "offline"
    assert data["latency_ms"] is None
    assert "refused" in data["last_error"].lower()


@patch("app.api.panels.requests.get")
def test_test_panel_timeout(mock_get, client, admin_token, db):
    panel = _make_panel(db, name="slow")
    mock_get.side_effect = __import__("requests").Timeout("timed out")

    resp = client.post(f"/api/panels/{panel.id}/test", headers=_auth(admin_token))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "offline"
    assert data["latency_ms"] is None
    assert "timed out" in data["last_error"].lower()


def test_test_panel_not_found(client, admin_token):
    resp = client.post("/api/panels/9999/test", headers=_auth(admin_token))
    assert resp.status_code == 404


@patch("app.api.panels.requests.get")
def test_test_panel_writes_last_poll_in_ms_on_success(mock_get, client, admin_token, db):

    panel = _make_panel(db, name="ok-panel")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_get.return_value = mock_resp

    resp = client.post(f"/api/panels/{panel.id}/test", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert resp.get_json()["last_poll"] >= 10**12, (
        f"last_poll={resp.get_json()['last_poll']} looks like seconds, not ms"
    )


@patch("app.api.panels.requests.get")
def test_test_panel_writes_last_poll_in_ms_on_connection_error(mock_get, client, admin_token, db):
    panel = _make_panel(db, name="refused-panel")
    mock_get.side_effect = __import__("requests").ConnectionError("refused")

    resp = client.post(f"/api/panels/{panel.id}/test", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert resp.get_json()["last_poll"] >= 10**12


@patch("app.api.panels.requests.get")
def test_test_panel_writes_last_poll_in_ms_on_timeout(mock_get, client, admin_token, db):
    panel = _make_panel(db, name="timeout-panel")
    mock_get.side_effect = __import__("requests").Timeout("timed out")

    resp = client.post(f"/api/panels/{panel.id}/test", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert resp.get_json()["last_poll"] >= 10**12
