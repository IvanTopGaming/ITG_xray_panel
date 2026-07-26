import json
import time

import jwt
import pytest

from panel_core.xray import gateway as gw
from panel_core.xray.local import LocalXrayGateway


def _reset_scheduler():
    from panel_core.extensions import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


@pytest.fixture(autouse=True)
def _scheduler_teardown():
    yield
    _reset_scheduler()


def _build_role_app(role, monkeypatch, tmp_path, name):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/{name}.db")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    from panel_core.roles import master, worker

    module = {"master": master, "worker": worker}[role]
    return module.create_app()


def _auth_headers():
    from panel_core.models import Admin
    from panel_core.utils import SECRET_KEY

    admin = Admin.query.first()
    token = jwt.encode(
        {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": int(admin.password_changed_at or 0),
            "exp": int(time.time()) + 3600,
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _seed_local_inbound_and_client(tag="node-tag", port=31000, email="alice"):
    from panel_core.extensions import db
    from panel_core.models import Client, Inbound

    db.session.add(
        Inbound(
            tag=tag,
            port=port,
            protocol="vless",
            stream_settings=json.dumps({"network": "tcp", "security": "none"}),
        )
    )
    db.session.add(
        Client(
            id="11111111-1111-1111-1111-111111111111",
            email=email,
            inbound_tag=tag,
            expiry_time=1000,
            limit_bytes=1000,
            enable=True,
            flow="",
        )
    )
    db.session.commit()


@pytest.fixture
def master_app(monkeypatch, tmp_path):
    return _build_role_app("master", monkeypatch, tmp_path, "master-crud")


@pytest.fixture
def worker_app(monkeypatch, tmp_path):
    return _build_role_app("worker", monkeypatch, tmp_path, "worker-crud")


LOCAL_INBOUND_CALLS = [
    ("post", "/api/inbounds", {"json": {"tag": "new-tag", "port": 31500, "protocol": "vless"}}),
    ("put", "/api/inbounds/node-tag", {"json": {"port": 31501}}),
    ("delete", "/api/inbounds/node-tag", {}),
]

LOCAL_USER_CALLS = [
    ("post", "/api/inbounds/node-tag/users", {"json": {"email": "bob"}}),
    ("put", "/api/inbounds/node-tag/users", {"json": {"old_email": "alice", "new_email": "bob"}}),
    ("delete", "/api/inbounds/node-tag/users?email=alice", {}),
]

LOCAL_BULK_CALLS = [
    ("/api/users/bulk-delete", {"users": [{"tag": "node-tag", "email": "alice"}]}),
    ("/api/users/bulk-enable", {"enable": False, "users": [{"tag": "node-tag", "email": "alice"}]}),
    ("/api/users/bulk-adjust-days", {"days": 5, "users": [{"tag": "node-tag", "email": "alice"}]}),
    ("/api/users/bulk-adjust-traffic", {"gb": 5, "users": [{"tag": "node-tag", "email": "alice"}]}),
    (
        "/api/users/bulk-set-flow",
        {"flow": "xtls-rprx-vision", "users": [{"tag": "node-tag", "email": "alice"}]},
    ),
]


@pytest.mark.parametrize("method,url,kwargs", LOCAL_INBOUND_CALLS, ids=[c[1] + c[0] for c in LOCAL_INBOUND_CALLS])
def test_master_refuses_local_inbound_crud(master_app, method, url, kwargs):
    from panel_core.extensions import db
    from panel_core.models import Inbound

    with master_app.app_context():
        _seed_local_inbound_and_client()
        client = master_app.test_client()

        resp = getattr(client, method)(url, headers=_auth_headers(), **kwargs)

        assert resp.status_code == 501, resp.get_data(as_text=True)
        assert "no local Xray instance" in resp.get_json()["error"]
        assert "panel_id" in resp.get_json()["error"]

        db.session.remove()
        assert {ib.tag for ib in Inbound.query.all()} == {"node-tag"}
        assert Inbound.query.filter_by(tag="node-tag").one().port == 31000


@pytest.mark.parametrize("method,url,kwargs", LOCAL_USER_CALLS, ids=[c[1] + c[0] for c in LOCAL_USER_CALLS])
def test_master_refuses_local_user_crud(master_app, method, url, kwargs):
    from panel_core.extensions import db
    from panel_core.models import Client

    with master_app.app_context():
        _seed_local_inbound_and_client()
        client = master_app.test_client()

        resp = getattr(client, method)(url, headers=_auth_headers(), **kwargs)

        assert resp.status_code == 501, resp.get_data(as_text=True)
        assert "no local Xray instance" in resp.get_json()["error"]

        db.session.remove()
        assert {c.email for c in Client.query.all()} == {"alice"}


@pytest.mark.parametrize("url,payload", LOCAL_BULK_CALLS, ids=[c[0] for c in LOCAL_BULK_CALLS])
def test_master_refuses_bulk_ops_that_carry_local_users(master_app, url, payload):
    from panel_core.extensions import db
    from panel_core.models import Client

    with master_app.app_context():
        _seed_local_inbound_and_client()
        client = master_app.test_client()

        resp = client.post(url, headers=_auth_headers(), json=payload)

        assert resp.status_code == 501, resp.get_data(as_text=True)
        assert "no local Xray instance" in resp.get_json()["error"]

        db.session.remove()
        untouched = Client.query.one()
        assert untouched.email == "alice"
        assert untouched.enable is True
        assert untouched.expiry_time == 1000
        assert untouched.limit_bytes == 1000
        assert untouched.flow == ""


def test_master_still_proxies_inbound_crud_to_a_node(master_app, monkeypatch):
    from panel_core.services import panel_proxy

    calls = []

    monkeypatch.setattr(
        panel_proxy,
        "proxy_create_inbound",
        lambda pid, data: calls.append(("create", pid, data)) or {"tag": data["tag"]},
    )
    monkeypatch.setattr(
        panel_proxy,
        "proxy_update_inbound",
        lambda pid, tag, data: calls.append(("update", pid, tag)) or {"status": "updated"},
    )
    monkeypatch.setattr(
        panel_proxy,
        "proxy_delete_inbound",
        lambda pid, tag: calls.append(("delete", pid, tag)) or {"status": "deleted"},
    )

    with master_app.app_context():
        client = master_app.test_client()
        headers = _auth_headers()

        created = client.post(
            "/api/inbounds?panel_id=7",
            headers=headers,
            json={"tag": "remote-tag", "port": 31600, "protocol": "vless"},
        )
        updated = client.put("/api/inbounds/remote-tag?panel_id=7", headers=headers, json={"port": 31601})
        deleted = client.delete("/api/inbounds/remote-tag?panel_id=7", headers=headers)

    assert created.status_code == 200, created.get_data(as_text=True)
    assert updated.status_code == 200, updated.get_data(as_text=True)
    assert deleted.status_code == 200, deleted.get_data(as_text=True)
    assert [c[0] for c in calls] == ["create", "update", "delete"]
    assert {c[1] for c in calls} == {7}


def test_master_still_proxies_user_crud_to_a_node(master_app, monkeypatch):
    from panel_core.services import panel_proxy

    calls = []

    monkeypatch.setattr(
        panel_proxy,
        "proxy_create_user",
        lambda pid, tag, data: calls.append(("create", pid, tag)) or {"email": data["email"]},
    )
    monkeypatch.setattr(
        panel_proxy,
        "proxy_update_user",
        lambda pid, tag, data: calls.append(("update", pid, tag)) or {"email": data["new_email"]},
    )
    monkeypatch.setattr(
        panel_proxy,
        "proxy_delete_user",
        lambda pid, tag, email: calls.append(("delete", pid, tag)) or {"status": "deleted"},
    )

    with master_app.app_context():
        client = master_app.test_client()
        headers = _auth_headers()

        created = client.post("/api/inbounds/remote-tag/users?panel_id=3", headers=headers, json={"email": "bob"})
        updated = client.put(
            "/api/inbounds/remote-tag/users?panel_id=3",
            headers=headers,
            json={"old_email": "bob", "new_email": "carol"},
        )
        deleted = client.delete("/api/inbounds/remote-tag/users?panel_id=3&email=carol", headers=headers)

    assert created.status_code == 200, created.get_data(as_text=True)
    assert updated.status_code == 200, updated.get_data(as_text=True)
    assert deleted.status_code == 200, deleted.get_data(as_text=True)
    assert [c[0] for c in calls] == ["create", "update", "delete"]
    assert {c[1] for c in calls} == {3}


BULK_PROXY_CASES = [
    ("/api/users/bulk-delete", "proxy_bulk_delete_users", {}, {"count": 1}),
    ("/api/users/bulk-enable", "proxy_bulk_enable_users", {"enable": False}, {"count": 1}),
    ("/api/users/bulk-adjust-days", "proxy_bulk_adjust_days", {"days": 5}, {"updated": 1, "skipped": 0}),
    ("/api/users/bulk-adjust-traffic", "proxy_bulk_adjust_traffic", {"gb": 5}, {"updated": 1, "skipped": 0}),
    (
        "/api/users/bulk-set-flow",
        "proxy_bulk_set_flow",
        {"flow": "xtls-rprx-vision"},
        {"updated": 1, "skipped": 0},
    ),
]


@pytest.mark.parametrize("url,proxy_name,extra,result", BULK_PROXY_CASES, ids=[c[0] for c in BULK_PROXY_CASES])
def test_master_still_fans_fully_remote_batches_out_to_nodes(master_app, monkeypatch, url, proxy_name, extra, result):
    from panel_core.services import panel_proxy

    calls = []
    monkeypatch.setattr(
        panel_proxy,
        proxy_name,
        lambda pid, group, *args: calls.append((pid, group)) or result,
    )

    payload = dict(extra)
    payload["users"] = [{"tag": "remote-tag", "email": "alice", "panel_id": 5}]

    with master_app.app_context():
        _seed_local_inbound_and_client()
        resp = master_app.test_client().post(url, headers=_auth_headers(), json=payload)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert len(calls) == 1
    assert calls[0][0] == 5
    assert calls[0][1] == [{"tag": "remote-tag", "email": "alice"}]


LOCAL_CONFIG_CALLS = [
    ("post", "/api/outbounds", {"json": {"tag": "ob1", "protocol": "freedom"}}),
    ("put", "/api/outbounds/ob1", {"json": {"enable": False}}),
    ("delete", "/api/outbounds/ob1", {}),
    ("post", "/api/balancers", {"json": {"tag": "bal1", "selector": ["direct"]}}),
    ("put", "/api/balancers/bal1", {"json": {"enable": False}}),
    ("delete", "/api/balancers/bal1", {}),
    ("post", "/api/routing-profiles", {"json": {"name": "rp1", "rules": []}}),
    ("put", "/api/routing-profiles/1", {"json": {"name": "rp2"}}),
    ("delete", "/api/routing-profiles/1", {}),
    ("post", "/api/user/routing", {"json": {"email": "alice", "outbound_tag": "direct"}}),
]


@pytest.mark.parametrize("method,url,kwargs", LOCAL_CONFIG_CALLS, ids=[f"{c[0]}{c[1]}" for c in LOCAL_CONFIG_CALLS])
def test_master_refuses_local_xray_config_endpoints(master_app, method, url, kwargs):
    with master_app.app_context():
        _seed_local_inbound_and_client()
        resp = getattr(master_app.test_client(), method)(url, headers=_auth_headers(), **kwargs)

    assert resp.status_code == 501, resp.get_data(as_text=True)
    assert "no local Xray instance" in resp.get_json()["error"]


def test_worker_still_runs_the_same_local_crud(worker_app):
    from panel_core.extensions import db
    from panel_core.models import Client, Inbound, Outbound, RoutingProfile

    calls = []

    class _Recording(LocalXrayGateway):
        def apply_config(self, validate=True):
            calls.append("apply_config")

        def restart(self):
            calls.append("restart")

        def add_user(self, inbound_tag, client_obj):
            calls.append("add_user")
            return True

        def remove_user(self, inbound_tag, email):
            calls.append("remove_user")
            return True

    with worker_app.app_context():
        gw.set_xray_gateway(_Recording())
        client = worker_app.test_client()
        headers = _auth_headers()

        created_inbound = client.post(
            "/api/inbounds",
            headers=headers,
            json={"tag": "node-tag", "port": 31000, "protocol": "vless"},
        )
        created_user = client.post("/api/inbounds/node-tag/users", headers=headers, json={"email": "alice"})
        adjusted = client.post(
            "/api/users/bulk-adjust-days",
            headers=headers,
            json={"days": 5, "users": [{"tag": "node-tag", "email": "alice"}]},
        )
        created_outbound = client.post("/api/outbounds", headers=headers, json={"tag": "ob1", "protocol": "freedom"})
        created_profile = client.post("/api/routing-profiles", headers=headers, json={"name": "rp1", "rules": []})
        routed = client.post("/api/user/routing", headers=headers, json={"email": "alice", "outbound_tag": "direct"})

        assert created_inbound.status_code == 201, created_inbound.get_data(as_text=True)
        assert created_user.status_code == 201, created_user.get_data(as_text=True)
        assert adjusted.status_code == 200, adjusted.get_data(as_text=True)
        assert created_outbound.status_code == 201, created_outbound.get_data(as_text=True)
        assert created_profile.status_code == 201, created_profile.get_data(as_text=True)
        assert routed.status_code == 200, routed.get_data(as_text=True)

        db.session.remove()
        assert Inbound.query.filter_by(tag="node-tag").count() == 1
        assert Client.query.filter_by(email="alice").count() == 1
        assert Outbound.query.filter_by(tag="ob1").count() == 1
        assert RoutingProfile.query.filter_by(name="rp1").count() == 1
        assert "apply_config" in calls
