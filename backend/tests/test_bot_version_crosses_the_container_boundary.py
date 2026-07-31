"""§67: the bot's version is written by one container and read by another.

`X-Bot-Version` arrives on `GET /bot/runtime-config`, which only **bot-api** serves. The only reader
is `GET /api/system/version`, which ships from `panel-adminapi` and therefore runs on the **master**.
Until this wave the value in between was a dict at module level, so the writer filled a variable in
one process and the reader looked at an empty one in another: System -> About showed no bot row at
all, alive or dead, and had done since the split.

This cannot be proved in one process (§58, §60). Two role apps in one interpreter share every module
global, so a dict would satisfy any assertion made here. Both tests below therefore break the shared
state on purpose:

- the role test reloads the `bot_status` module between the write and the read, which throws away any
  process-local memory while leaving the stand-in shared tier intact;
- the second test asserts the module simply holds no mutable module-level state to fall back on.

The live cross-process run is recorded in INFRA.md's wave 5b section.
"""

from __future__ import annotations

import ast
import datetime
import importlib

import jwt as jwt_lib
import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Admin, SystemSetting
from panel_core.utils import SECRET_KEY

from tests.import_graph import source_path
from tests.schema import ensure_schema


BOT_TOKEN = "wave5b-runtime-config-token"


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    try:
        scheduler.remove_all_jobs()
    except Exception:
        pass


@pytest.fixture
def data_tier(monkeypatch):
    """Stands in for the shared Redis: one store, reachable from both role apps."""

    store = {}

    class _FakeRedis:
        def setex(self, key, ttl, value):
            store[key] = value

        def get(self, key):
            return store.get(key)

    monkeypatch.setattr("panel_core.extensions.get_shared_redis", lambda: _FakeRedis())
    monkeypatch.setattr("panel_core.services.bot_status.get_shared_redis", lambda: _FakeRedis())
    return store


def _build(role_module, role_env, monkeypatch, tmp_path, name):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", role_env)
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/{name}.db"))
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.com")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)
    return importlib.import_module(role_module).create_app()


@pytest.fixture
def botapi_app(monkeypatch, tmp_path):
    app = _build("panel_core.roles.botapi", "bot", monkeypatch, tmp_path, "botapi")
    with app.app_context():
        db.session.add(SystemSetting(key="bot_service_token", value=BOT_TOKEN))
        db.session.commit()
    return app


@pytest.fixture
def master_app(monkeypatch, tmp_path):
    app = _build("panel_core.roles.master", "master", monkeypatch, tmp_path, "master")
    with app.app_context():
        if Admin.query.first() is None:
            db.session.add(Admin(username="admin", password="x", password_changed_at=0))
            db.session.commit()
    return app


@pytest.fixture
def master_headers(master_app):
    with master_app.app_context():
        admin = Admin.query.first()
        payload = {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": int(admin.password_changed_at or 0),
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
        }
    return {"Authorization": f"Bearer {jwt_lib.encode(payload, SECRET_KEY, algorithm='HS256')}"}


def _forget_everything_this_process_remembers():
    """The process boundary, as far as one interpreter can imitate it.

    Deliberately generic: it empties **every** mutable module-level container in `bot_status`,
    whatever it is called, so a revert to the old `_STATE` dict is wiped between the write and the
    read exactly as a container restart would wipe it. Reloading the module instead does nothing
    useful -- `api/system.py` imported `get_bot_status` by value, so it keeps calling the old
    function object over the old globals, and the mutation survives.
    """

    import panel_core.services.bot_status as bot_status

    for name, value in vars(bot_status).items():
        if name.startswith("__"):
            continue
        if isinstance(value, (dict, list, set)):
            value.clear()


def test_the_master_reads_the_version_bot_api_recorded(botapi_app, master_app, master_headers, data_tier):
    reported = botapi_app.test_client().get(
        "/api/bot/runtime-config",
        headers={"Authorization": f"Bearer {BOT_TOKEN}", "X-Bot-Version": "2.2.4"},
    )
    assert reported.status_code == 200, reported.get_data(as_text=True)

    _forget_everything_this_process_remembers()

    resp = master_app.test_client().get("/api/system/version", headers=master_headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["running"]["bot"] == "2.2.4", (
        "the master did not see the version bot-api recorded. Writer and reader are different "
        "containers on different machines; anything that only works when they share a process "
        "leaves System -> About with no bot row forever, which is what shipped until wave 5b."
    )
    assert body["running"]["bot_reported_at"] is not None


def test_the_master_shows_nothing_when_the_bot_has_not_reported(master_app, master_headers, data_tier):
    resp = master_app.test_client().get("/api/system/version", headers=master_headers)
    assert resp.status_code == 200
    assert resp.get_json()["running"]["bot"] is None, (
        "with no report in the shared tier the master must answer None -- the UI hides the row on a "
        "falsy value, so inventing a version here would put a permanently-current bot on the page."
    )


def test_bot_status_keeps_no_process_local_state_to_fall_back_on():
    """Belt and braces for the test above: a module-level dict would make it pass vacuously in a
    single-process test run while the split deployment stayed broken."""

    tree = ast.parse(source_path("services/bot_status.py").read_text())
    mutable_globals = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if isinstance(node.value, (ast.Dict, ast.List, ast.Set)):
            mutable_globals.extend(t.id for t in node.targets if isinstance(t, ast.Name))
    assert mutable_globals == [], (
        f"services/bot_status.py declares mutable module state {mutable_globals}. That is exactly the "
        f"defect §67 recorded: a dict here is filled on bot-api and read as empty on the master."
    )

    import panel_core.services.bot_status as bot_status

    assert "get_shared_redis" in source_path("services/bot_status.py").read_text(), (
        "the assertion above would also pass on a module that stopped carrying the version at all"
    )
    assert hasattr(bot_status, "STATUS_KEY")
