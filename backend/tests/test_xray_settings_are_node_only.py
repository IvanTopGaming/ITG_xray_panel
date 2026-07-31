"""§68 + §70: the master stops answering for an Xray it does not run.

Three handlers in `api/system.py` had no `has_local_xray()` gate while every neighbour in the same
file had one:

- `PUT /api/system/settings` wrote `xray_log_level` / `geoip_url` / `geosite_url` into the master's
  Postgres and answered **200 with the updated form**. The only reader of those keys is
  `generate_config_file()`, which runs on a node against that node's own SQLite, and the restart the
  handler then triggers goes through `RemoteXrayGateway` and returns `None`. Nothing changed
  anywhere, and nothing said so -- §10.4's "the setting does not live where its consumer does", with
  the half that matters here being the claim of success.
- `GET /api/system/settings` answered with the master's own copy of the same dead keys.
- `GET /api/config` read `/etc/xray/config.json`, which the `panel-master` image does not contain,
  and answered `404 "Config file not found"` -- loud, but misleading in the same way `"DB not found"`
  was in §7.10: the config is alive and well, on the node.

What this file pins is the **refusal**, not a capability: an admin still cannot set a node's log
level from the master. Wave 5c is where `?panel_id=` dispatch turns that into a capability, and it
will sit *above* this gate, the shape waves 4c-2 and 4d already established -- so nothing here is
written twice.

Every handler is asserted individually rather than by sample (§80): the property is held by a line
per handler, and a line per handler is what gets reverted one at a time.
"""

from __future__ import annotations

import datetime
import importlib
import json
import os

import jwt as jwt_lib
import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Admin, SystemSetting
from panel_core.utils import SECRET_KEY

from tests.schema import ensure_schema


NODE_ONLY_READS = (
    "/api/system/settings",
    "/api/config",
)


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    try:
        scheduler.remove_all_jobs()
    except Exception:
        pass


def _admin_headers(app):
    with app.app_context():
        admin = Admin.query.first()
        if admin is None:
            admin = Admin(username="admin", password="x", password_changed_at=0)
            db.session.add(admin)
            db.session.commit()
        payload = {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": int(admin.password_changed_at or 0),
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
        }
    return {"Authorization": f"Bearer {jwt_lib.encode(payload, SECRET_KEY, algorithm='HS256')}"}


@pytest.fixture
def master_app(monkeypatch, tmp_path):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/master.db"))
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)
    return importlib.import_module("panel_core.roles.master").create_app()


@pytest.fixture
def master(master_app):
    return master_app.test_client()


@pytest.fixture
def master_headers(master_app):
    return _admin_headers(master_app)


@pytest.fixture
def node_app(monkeypatch, tmp_path):
    """No `DATABASE_URL`, matching docker-compose.node.yml (§48)."""

    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "worker")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)
    return importlib.import_module("panel_core.roles.worker").create_app()


@pytest.fixture
def node(node_app):
    return node_app.test_client()


@pytest.fixture
def node_headers(node_app):
    return _admin_headers(node_app)


@pytest.mark.parametrize("path", NODE_ONLY_READS)
def test_the_master_refuses_to_read_a_config_it_does_not_have(master, master_headers, path):
    resp = master.get(path, headers=master_headers)
    assert resp.status_code == 501, (
        f"{path} answered {resp.status_code} on the master. The Xray whose settings and config these "
        f"are runs on a node; a 200 here hands the admin a value nothing reads, and the 404 "
        f"/api/config used to give reads as 'your config is gone'.\n\n{resp.get_data(as_text=True)}"
    )
    assert "no local Xray" in resp.get_json()["error"]


def test_the_master_refuses_to_save_a_setting_that_would_reach_nothing(master, master_headers, master_app):
    resp = master.put("/api/system/settings", headers=master_headers, json={"xrayLogLevel": "debug"})
    assert resp.status_code == 501, resp.get_data(as_text=True)
    assert "no local Xray" in resp.get_json()["error"]

    with master_app.app_context():
        stored = SystemSetting.query.filter_by(key="xray_log_level").first()
    assert stored is None, (
        "the refusal must come before the write. A 501 that still stored the value would leave a row "
        "in the master's Postgres that nothing on any node ever reads -- the same lie one layer down."
    )


def test_a_node_still_reads_and_saves_its_own_xray_settings(node, node_headers, node_app):
    """The negative control: without this, moving the gate to every role would look identical."""

    read = node.get("/api/system/settings", headers=node_headers)
    assert read.status_code == 200, read.get_data(as_text=True)
    assert "xrayLogLevel" in read.get_json()

    with (
        pytest.MonkeyPatch.context() as mp,
    ):
        mp.setattr("panel_core.api.system.generate_config_file", lambda *a, **k: None)
        mp.setattr("panel_core.api.system.restart_xray_container", lambda *a, **k: None)
        saved = node.put("/api/system/settings", headers=node_headers, json={"xrayLogLevel": "debug"})

    assert saved.status_code == 200, saved.get_data(as_text=True)
    assert saved.get_json()["xrayLogLevel"] == "debug"
    with node_app.app_context():
        assert SystemSetting.query.filter_by(key="xray_log_level").first().value == "debug"


def test_a_node_serves_its_own_xray_config_file(node, node_headers, tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"inbounds": [{"tag": "vless-reality"}]}))
    real_exists, real_open = os.path.exists, open

    monkeypatch.setattr(
        "panel_core.api.system.os.path.exists",
        lambda p: True if p == "/etc/xray/config.json" else real_exists(p),
    )
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(str(config), *a, **k) if p == "/etc/xray/config.json" else real_open(p, *a, **k),
    )

    resp = node.get("/api/config", headers=node_headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["inbounds"][0]["tag"] == "vless-reality"


def test_a_node_with_no_config_file_yet_still_says_so_rather_than_501(node, node_headers, monkeypatch):
    """404 stays the right answer where the file genuinely could exist and does not."""

    monkeypatch.setattr("panel_core.api.system.os.path.exists", lambda p: False)
    resp = node.get("/api/config", headers=node_headers)
    assert resp.status_code == 404
