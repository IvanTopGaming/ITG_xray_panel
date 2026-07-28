"""§25: the bot service token stops opening the admin API.

Seven endpoints accepted the bot's token alongside an admin JWT -- `GET /api/inbounds` and six
handlers in `panels.py`. All seven live only on the master, and the bot, the only holder of that
token, talks to bot-api, where none of them exists. So the bot-token branch of those seven was
unreachable: extra surface with no user.

What kept it unreachable was `tg_bot/api_service.py` being broken, not a decision. Wave 4a deletes
that file for good and closes the branch with it: the token now reaches `/bot-service/*` and
`/billing/checkout` and nothing else. Concretely, a leaked bot token no longer reads every inbound
with its clients' UUIDs, restarts nodes, or downloads their backups.
"""

import datetime

import jwt
import pytest

from panel_core.extensions import db
from panel_core.models import SystemSetting
from panel_core.utils import SECRET_KEY

from tests.schema import ensure_schema


BOT_TOKEN = "bot-token-scope-abc"

NARROWED_ENDPOINTS = (
    ("GET", "/api/inbounds"),
    ("GET", "/api/panels"),
)

WRITE_ENDPOINTS = (
    ("POST", "/api/inbounds"),
    ("POST", "/api/inbounds/probe/users"),
    ("PUT", "/api/inbounds/probe"),
    ("DELETE", "/api/inbounds/probe"),
    ("POST", "/api/users/bulk-delete"),
    ("POST", "/api/users/bulk-enable"),
    ("POST", "/api/users/bulk-adjust-days"),
    ("POST", "/api/users/bulk-adjust-traffic"),
    ("POST", "/api/users/bulk-set-flow"),
    ("POST", "/api/users/reset-traffic"),
    ("POST", "/api/restart"),
    ("GET", "/api/stats/system"),
)


@pytest.fixture
def master_app(monkeypatch, tmp_path):
    """The master role, because all seven narrowed endpoints live there and nowhere else."""

    from panel_core.extensions import scheduler
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/master.db"))
    monkeypatch.chdir(tmp_path)
    gw.set_xray_gateway(None)

    import importlib

    app = importlib.import_module("panel_core.roles.master").create_app()

    with app.app_context():
        row = SystemSetting.query.filter_by(key="bot_service_token").first()
        if row is None:
            row = SystemSetting(key="bot_service_token")
            db.session.add(row)
        row.value = BOT_TOKEN
        db.session.commit()

    yield app

    if scheduler.running:
        scheduler.shutdown(wait=False)


@pytest.fixture
def client(master_app):
    return master_app.test_client()


@pytest.fixture
def admin_token(master_app):
    with master_app.app_context():
        from panel_core.models import Admin

        admin = Admin.query.first()
        assert admin is not None, "bootstrap_defaults is expected to seed an admin row on the master"
        return jwt.encode(
            {
                "admin_id": admin.id,
                "role": "admin",
                "pwdv": admin.password_changed_at or 0,
                "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
            },
            SECRET_KEY,
            algorithm="HS256",
        )


@pytest.mark.parametrize("method,path", NARROWED_ENDPOINTS)
def test_the_bot_service_token_is_refused_by_the_admin_api(client, method, path):
    resp = client.open(path, method=method, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
    assert resp.status_code == 401, (
        f"{method} {path} accepted the bot service token (HTTP {resp.status_code}). That token lives in "
        "SystemSetting and is handed to the bot container; it must not read inbounds with client UUIDs "
        "or reach the panel-management surface."
    )


@pytest.mark.parametrize("method,path", NARROWED_ENDPOINTS)
def test_an_admin_jwt_still_passes(client, admin_token, method, path):
    resp = client.open(path, method=method, headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, (
        f"{method} {path} refused an admin JWT (HTTP {resp.status_code}) -- the narrowing closed the "
        "endpoint to its real user, not just to the bot."
    )


def test_the_bot_service_token_still_opens_its_own_surface(app):
    """/bot-service/* is the whole surface the token is for; narrowing the admin API must not touch it.

    This one runs on the generic app fixture rather than the master role deliberately: the master
    registers no `bot_service` blueprint at all since phase 3c-2, so there is no master-shaped app
    on which the positive case could be asked.
    """

    from panel_core.api import bot_service

    if "bot_service" not in app.blueprints:
        app.register_blueprint(bot_service.bp, url_prefix="/api")

    with app.app_context():
        row = SystemSetting.query.filter_by(key="bot_service_token").first()
        if row is None:
            row = SystemSetting(key="bot_service_token")
            db.session.add(row)
        row.value = BOT_TOKEN
        db.session.commit()

    resp = app.test_client().get("/api/bot-service/users/12345/state", headers={"Authorization": f"Bearer {BOT_TOKEN}"})
    assert resp.status_code == 200, (
        "the bot service token must keep working on /bot-service/* -- that is the whole surface it is "
        f"for, and narrowing the admin API must not touch it (HTTP {resp.status_code})"
    )


@pytest.mark.parametrize("method,path", WRITE_ENDPOINTS)
def test_the_bot_service_token_cannot_write_through_the_federation_decorator(client, method, path):
    """The larger half of the surface, which §25 did not count.

    `admin_or_federation_token_required` carried a third branch of its own — it accepted the bot
    service token alongside an admin JWT and a federation token. That branch arrived with the
    multi-panel federation release, when inbound CRUD moved onto this decorator and the bot still
    talked to the monolith's admin API directly.

    So a leaked bot token did not merely read inbounds and restart nodes (the seven endpoints §25
    counted): it could create and delete any user and any inbound, on the master and through it on
    every node, plus run all six batch operations. Verified by running, not by reading — none of
    these returned 401 before the branch was removed; they returned 501 from the local-Xray gate,
    400 from body validation, or 200.
    """

    resp = client.open(path, method=method, json={}, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
    assert resp.status_code == 401, (
        f"{method} {path} accepted the bot service token (HTTP {resp.status_code}). Anything other than "
        "401 means authorization passed — a 501 from the has_local_xray gate or a 400 from validation "
        "is still a pass."
    )


def test_the_bot_token_check_has_no_consumer_left():
    """`bot_service_token_required` compares the token itself; the shared helper had one user."""

    from tests.import_graph import iter_sources

    offenders = sorted(str(path) for path in iter_sources() if "_check_bot_service_token" in path.read_text())
    assert offenders == [], (
        f"_check_bot_service_token survives in {offenders}. Its only consumer was the bot-token branch of "
        "admin_or_federation_token_required, removed in wave 4a."
    )


def test_the_dual_decorator_is_gone_from_the_workspace():
    """A decorator with no consumer is an invitation to acquire one.

    Leaving `admin_or_bot_token_required` defined would keep the surface one import away, and the
    endpoints it guarded are exactly the ones worth keeping shut: inbound listing with client UUIDs,
    node restart, node backup and node restore.
    """

    from tests.import_graph import iter_sources

    offenders = sorted(str(path) for path in iter_sources() if "admin_or_bot_token_required" in path.read_text())
    assert offenders == [], (
        f"admin_or_bot_token_required is still present in {offenders}. Wave 4a removed its last consumer; "
        "the definition goes with it."
    )
