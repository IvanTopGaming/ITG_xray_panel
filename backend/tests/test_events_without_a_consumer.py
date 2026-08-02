"""§69: nothing publishes to `bot:events` that the bot has no branch for.

`config_changed` (admin saves Bot -> Settings) and `trial_activated` (user claims the trial) were
both published onto the shared bus and both fell straight into the consumer's `else: return`. Each
publication also wrote a row into `bot_event`, where it sat until `cleanup_bot_events` pruned it.
Neither was needed: the bot re-reads its runtime config every 60 seconds by itself, and it shows the
trial's outcome from the HTTP response it is already waiting on.

The assertion that holds nothing is "the string `config_changed` no longer appears in the source" --
it is green if the publication is removed and equally green if the whole module is deleted. So this
file checks the two ends that matter:

1. **the producer** -- the admin action runs and leaves **no `bot_event` row**, which is what a
   publication writes first and unconditionally;
2. **the consumer** -- the bot's dispatcher has no branch for either type, so re-adding a
   publication would still deliver nothing.
"""

from __future__ import annotations

import ast
import datetime
import importlib
import pathlib

import jwt as jwt_lib
import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Admin, BotEvent, SystemSetting, Tariff, TariffItem, TelegramUser
from panel_core.utils import SECRET_KEY

from tests.schema import ensure_schema


BOT_TOKEN = "wave5b-trial-token"
CONSUMER = pathlib.Path(__file__).resolve().parents[2] / "tg_bot" / "bot_events_consumer.py"


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    try:
        scheduler.remove_all_jobs()
    except Exception:
        pass


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


@pytest.fixture
def botapi_app(monkeypatch, tmp_path):
    app = _build("panel_core.roles.botapi", "bot", monkeypatch, tmp_path, "botapi")
    with app.app_context():
        db.session.add(SystemSetting(key="bot_service_token", value=BOT_TOKEN))
        trial = Tariff(name="Trial", price_rub=0, period_days=3, is_trial=True, enabled=True)
        db.session.add(trial)
        db.session.flush()
        db.session.add(TariffItem(tariff_id=trial.id, inbound_tag="ams-reality", traffic_gb=0, panel_id=5))
        db.session.add(TelegramUser(telegram_id=88, language="ru"))
        db.session.commit()
    return app


def _consumer_event_types():
    """Every `etype == "..."` / `etype in (...)` the bot's dispatcher branches on."""

    tree = ast.parse(CONSUMER.read_text())
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name) or node.left.id != "etype":
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                found.add(comparator.value)
            elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                found.update(e.value for e in comparator.elts if isinstance(e, ast.Constant))
    return found


def test_saving_bot_settings_writes_no_event(master_app, master_headers):
    resp = master_app.test_client().put(
        "/api/bot/settings",
        headers=master_headers,
        json={"display_timezone": "Europe/Berlin"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    with master_app.app_context():
        assert SystemSetting.query.filter_by(key="display_timezone").first().value == "Europe/Berlin", (
            "the setting itself must still be saved -- otherwise this test would pass on a handler "
            "that stopped working altogether"
        )
        rows = [e.type for e in BotEvent.query.all()]
    assert rows == [], (
        f"saving bot settings wrote {rows} into bot_event. `config_changed` had no branch in the bot's "
        f"consumer, so every save left a row nobody would ever act on; the bot re-reads its runtime "
        f"config on its own 60-second poll."
    )


def test_claiming_the_trial_writes_no_event(botapi_app, monkeypatch):
    monkeypatch.setattr(
        "panel_core.services.panel_proxy.proxy_provision",
        lambda panel_id, tg, tag, payload: {"expires_at_ms": 1_800_000_000_000, "client": {}},
    )
    resp = botapi_app.test_client().post(
        "/api/bot-service/trial/activate",
        headers={"Authorization": f"Bearer {BOT_TOKEN}"},
        json={"telegram_id": 88},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["expires_at_ms"] == 1_800_000_000_000, (
        "the trial must still be granted and still report its date -- that response is where the bot "
        "gets the text it shows, and it is why the event was never needed"
    )

    with botapi_app.app_context():
        rows = [e.type for e in BotEvent.query.all()]
    assert rows == [], f"claiming the trial wrote {rows} into bot_event; `trial_activated` has no consumer"


def test_an_event_the_bot_does_act_on_is_still_written(master_app, master_headers):
    """The positive control. "No rows in bot_event" is also what a broken table, a dead publisher or
    a mis-seeded fixture looks like; blocking a user is a change the bot has a branch for, and it has
    to keep landing."""

    with master_app.app_context():
        db.session.add(TelegramUser(telegram_id=77, language="ru"))
        db.session.commit()

    resp = master_app.test_client().post("/api/bot/users/77/block", headers=master_headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    with master_app.app_context():
        assert [e.type for e in BotEvent.query.all()] == ["user_blocked"]


@pytest.mark.parametrize("etype", ["config_changed", "trial_activated"])
def test_the_bot_has_no_branch_for_the_removed_types(etype):
    """The other end. Without this the producer tests above would still pass if someone re-added a
    publication *and* a consumer branch, which is a different (defensible) decision -- this is what
    makes the pair a statement about the removal rather than about one side of it."""

    assert etype not in _consumer_event_types()


def test_the_branch_scan_actually_finds_branches():
    """Guards the guard: an AST walk that matched nothing would make the test above vacuous."""

    found = _consumer_event_types()
    assert {"payment_succeeded", "texts_changed", "expiry_notification"} <= found, found
