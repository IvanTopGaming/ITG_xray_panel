"""§19 + §7.10 + §8.8/§8.14: backing a node up from the master, and a master that stops lying.

Two defects, both present since `2a7694e feat: initial release v1.0.0`, and they are opposite in
character.

`panels.py` proxies `GET /api/backup` and `POST /api/restore` to the node with an
`X-Federation-Token` and no `Authorization` header at all, while the node guarded both with
`token_required` — admin JWT only. So every backup of a node taken from the master answered 401,
and the capability §8.8 assumed was "half done" did not exist on any path.

The master's own pair was worse than useless. `/api/backup` looked for a SQLite file that a
Postgres role does not have and answered `404 "DB not found"` — a message that reads as "your
database is gone". `/api/restore` accepted a node's backup, tore down the live Postgres connection
pool, wrote the file where nothing reads it, restarted the worker and answered
`{"status": "restored"}` with HTTP 200. That is a disaster-recovery path telling an admin who has
already lost something that it came back.

The decisive assertions are therefore:

* the node **hands over the file** to a federation token — not "the master refuses politely", which
  is what a guard written around the gate alone would prove while proxying stayed broken (§8.14
  fixes authorisation first for exactly this reason);
* the master answers **404** on both, because the routes now live in a blueprint only
  `roles/worker.py` registers;
* a role that keeps its data in Postgres answers 409 and **leaves the file alone** — the node is
  the only role that still serves these routes, and `docker-compose.node.yml` merely omits
  `DATABASE_URL` rather than forbidding it, so this is the last reachable way to reproduce §7.10.

The node side builds the worker role app **without `DATABASE_URL`** (§48): setting one points the
ORM at a different file from the one `migrate_sqlite_db` writes, and the assertions would read an
empty database and pass for the wrong reason.

The negative credential is a **real** one (`bot_service_token` is written into the node's own
database before the request), because a key that would have been rejected anyway proves nothing —
that is how the wave-4b assertion passed against a mutated decorator.
"""

import datetime
import logging
import os
import sqlite3
import time

import jwt as jwt_lib
import pytest

from panel_core.extensions import db
from panel_core.models import Admin, SystemSetting
from panel_core.utils import SECRET_KEY

from tests.schema import ensure_schema


BOT_TOKEN = "bot-token-must-not-open-the-backup"
POSTGRES_URI = "postgresql+psycopg2://panel:pw@data.example.com/panel?sslmode=verify-full"


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


@pytest.fixture(autouse=True)
def _keep_restore_inside_the_test_process(monkeypatch):
    """Two things `restore` does to its host, neither of which this file is about.

    It SIGTERMs its own process a second later — unpatched that ends the pytest run — and it
    restarts the Xray container through the Docker socket, which no test host has, so the handler's
    bare `except` would turn every restore into a 500 and hide what the file actually asserts.
    Patched where they are used, not where they are defined.
    """

    monkeypatch.setattr("panel_core.api.backup._schedule_worker_restart", lambda *a, **kw: None)
    monkeypatch.setattr("panel_core.api.backup.restart_xray_container", lambda *a, **kw: None)


def _admin_headers(app):
    with app.app_context():
        admin = Admin.query.filter_by(username="admin").first()
        if admin is None:
            admin = Admin(username="admin", password="x", password_changed_at=0)
            db.session.add(admin)
            db.session.commit()
        pwd_version = int(admin.password_changed_at or 0)
        admin_id = admin.id

    token = jwt_lib.encode(
        {
            "user": "admin",
            "admin_id": admin_id,
            "role": "admin",
            "pwdv": pwd_version,
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def node_app(monkeypatch, tmp_path):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "worker")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    import importlib

    app = importlib.import_module("panel_core.roles.worker").create_app()

    with app.app_context():
        db.session.add(SystemSetting(key="bot_service_token", value=BOT_TOKEN))
        db.session.commit()

    return app


@pytest.fixture
def node(node_app):
    return node_app.test_client()


@pytest.fixture
def node_headers(node_app):
    return _admin_headers(node_app)


@pytest.fixture
def federation_token(node, node_headers):
    """The real linking procedure, so the token the master would send is the token under test."""

    import base64

    issued = node.post("/api/federation/link-token", headers=node_headers)
    assert issued.status_code == 200
    composite = issued.get_json()["link_token"]
    raw = base64.urlsafe_b64decode(composite + "=" * (-len(composite) % 4)).decode().split("|", 1)[1]

    shaken = node.post(
        "/api/federation/handshake",
        json={"link_token": raw, "master_url": "https://master.example.com/", "master_name": "Master"},
    )
    assert shaken.status_code == 200
    return shaken.get_json()["federation_token"]


def _a_valid_sqlite_file(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO probe (id) VALUES (1)")
    conn.commit()
    conn.close()
    return path


class TestTheMasterCanFinallyBackUpANode:
    def test_the_node_hands_its_database_over_the_federation_token(self, node, federation_token):
        resp = node.get("/api/backup", headers={"X-Federation-Token": federation_token})

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.data.startswith(b"SQLite format 3\x00")

    def test_a_backup_taken_from_the_master_can_be_put_back_from_the_master(self, node, federation_token, tmp_path):
        """The whole round trip of §8.14 step 3, over the credential the master actually sends."""

        taken = node.get("/api/backup", headers={"X-Federation-Token": federation_token})
        assert taken.status_code == 200
        downloaded = tmp_path / "downloaded.db"
        downloaded.write_bytes(taken.data)

        with downloaded.open("rb") as handle:
            resp = node.post(
                "/api/restore",
                headers={"X-Federation-Token": federation_token},
                data={"file": (handle, "node.db")},
                content_type="multipart/form-data",
            )

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json() == {"status": "restored"}

    def test_an_admin_logged_into_the_node_itself_still_gets_the_backup(self, node, node_headers):
        resp = node.get("/api/backup", headers=node_headers)

        assert resp.status_code == 200
        assert resp.data.startswith(b"SQLite format 3\x00")

    def test_a_revoked_federation_token_stops_getting_the_backup(self, node, node_headers, federation_token):
        assert node.get("/api/backup", headers={"X-Federation-Token": federation_token}).status_code == 200

        node.post("/api/federation/link-token", headers=node_headers)

        assert node.get("/api/backup", headers={"X-Federation-Token": federation_token}).status_code == 401

    def test_the_bot_service_token_does_not_open_the_node_backup(self, node, node_app):
        with node_app.app_context():
            stored = SystemSetting.query.filter_by(key="bot_service_token").first()
            assert stored is not None and stored.value == BOT_TOKEN, "the rejected key must be a real one"

        assert node.get("/api/backup", headers={"Authorization": f"Bearer {BOT_TOKEN}"}).status_code == 401
        assert node.post("/api/restore", headers={"Authorization": f"Bearer {BOT_TOKEN}"}).status_code == 401
        assert node.get("/api/backup", headers={"X-Federation-Token": BOT_TOKEN}).status_code == 401

    def test_handing_the_database_over_the_wire_is_written_down(self, node, federation_token, caplog):
        with caplog.at_level(logging.WARNING, logger="panel_core.api.backup"):
            node.get("/api/backup", headers={"X-Federation-Token": federation_token})

        federated = [r for r in caplog.records if "federation" in r.getMessage()]
        assert federated, "a federated backup must leave a WARNING naming the credential it used"

    def test_an_admin_of_the_node_does_not_raise_the_same_alarm(self, node, node_headers, caplog):
        with caplog.at_level(logging.WARNING, logger="panel_core.api.backup"):
            node.get("/api/backup", headers=node_headers)

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestTheMasterServesNoBackupOfItsOwn:
    @pytest.fixture
    def master_app(self, monkeypatch, tmp_path):
        from panel_core.xray import gateway as gw

        monkeypatch.setenv("PANEL_ROLE", "master")
        monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/master.db"))
        monkeypatch.chdir(tmp_path)
        _reset_scheduler()
        gw.set_xray_gateway(None)

        import importlib

        return importlib.import_module("panel_core.roles.master").create_app()

    @pytest.fixture
    def master(self, master_app):
        return master_app.test_client()

    def test_the_master_has_no_backup_route_at_all(self, master, master_app):
        """404 alone would pass for the wrong reason: `_db_path()` misses too, and answers 404.

        A client cannot tell "no such route" from "no such file", which is precisely why §7.10's
        `404 DB not found` read as "your database is gone". So the route table is asserted as well.
        """

        served = {str(rule) for rule in master_app.url_map.iter_rules()}
        assert "/api/backup" not in served
        assert "/api/restore" not in served

        resp = master.get("/api/backup", headers=_admin_headers(master_app))
        assert resp.status_code == 404

    def test_the_master_no_longer_claims_to_have_restored_anything(self, master, master_app, tmp_path):
        uploaded = _a_valid_sqlite_file(str(tmp_path / "someone-elses.db"))

        with open(uploaded, "rb") as handle:
            resp = master.post(
                "/api/restore",
                headers=_admin_headers(master_app),
                data={"file": (handle, "node.db")},
                content_type="multipart/form-data",
            )

        assert resp.status_code == 404
        assert resp.get_json() != {"status": "restored"}

    def test_a_node_that_refuses_the_token_says_so_instead_of_backup_failed(self, master, master_app):
        """401 from a node means one thing: relink it (wave 4b). Saying "Backup failed" hides that."""

        from unittest.mock import MagicMock, patch

        from panel_core.models import LinkedPanel

        with master_app.app_context():
            panel = LinkedPanel(
                name="node-1",
                url="https://node-1.example.com",
                federation_token="stale",
                enable=True,
                created_at=int(time.time() * 1000),
            )
            db.session.add(panel)
            db.session.commit()
            panel_id = panel.id

        refused = MagicMock(status_code=401)
        refused.json.return_value = {"error": "invalid or missing federation token"}

        with patch("panel_core.api.panels.requests.get", return_value=refused):
            resp = master.get(f"/api/panels/{panel_id}/backup", headers=_admin_headers(master_app))

        assert resp.status_code == 401
        assert "relink" in resp.get_json()["error"].lower()


class TestAPostgresRoleRefusesInsteadOfPretending:
    def test_backup_refuses_when_the_role_keeps_its_data_in_postgres(self, node, node_app, federation_token):
        node_app.config["SQLALCHEMY_DATABASE_URI"] = POSTGRES_URI

        resp = node.get("/api/backup", headers={"X-Federation-Token": federation_token})

        assert resp.status_code == 409
        assert "pg-backup" in resp.get_json()["error"]

    def test_restore_refuses_and_leaves_the_database_untouched(self, node, node_app, federation_token, tmp_path):
        from panel_core.api.backup import _db_path

        with node_app.app_context():
            live_db = _db_path()
        before = os.stat(live_db).st_mtime_ns
        time.sleep(0.01)

        node_app.config["SQLALCHEMY_DATABASE_URI"] = POSTGRES_URI
        uploaded = _a_valid_sqlite_file(str(tmp_path / "upload.db"))

        with open(uploaded, "rb") as handle:
            resp = node.post(
                "/api/restore",
                headers={"X-Federation-Token": federation_token},
                data={"file": (handle, "node.db")},
                content_type="multipart/form-data",
            )

        assert resp.status_code == 409
        assert resp.get_json() != {"status": "restored"}
        assert os.stat(live_db).st_mtime_ns == before
