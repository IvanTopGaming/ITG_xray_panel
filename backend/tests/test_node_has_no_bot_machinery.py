"""§8.6: a node stops carrying bot machinery it cannot use.

Three things ran on a node for no reason, and one of them reached outside the node:

* a `bot_service_token` was generated into the node's own SQLite, while the `bot_service`
  blueprint is registered only by bot-api — a live credential nothing could ever present;
* ~74 bot texts × 2 languages were seeded into a table the node never reads;
* worse, a force-reseed **published `texts_changed` on the shared bus**, so restarting a node
  could make the bot re-read its texts against a version stored in that node's local table.

What stays is deliberate: `generate_config_file` and the `Admin` row. A node runs its own Xray
and its own login, so both belong there (see §8.6 — a separate admin per node is expected, not
a defect).
"""

import pytest

from tests.schema import ensure_schema


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


@pytest.fixture
def published(monkeypatch):
    events = []
    monkeypatch.setattr(
        "panel_core.services.bot_events.publish",
        lambda event_type, telegram_id=None, payload=None: events.append(event_type),
    )
    return events


def _build_worker(monkeypatch, tmp_path):
    """No DATABASE_URL, exactly as docker-compose.node.yml leaves it.

    Setting one here would point the ORM at a different file from the one `migrate_sqlite_db`
    writes to (it takes `app_base.db_path()`), and every assertion below would then read an empty
    database and pass for the wrong reason.
    """

    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "worker")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    import importlib

    return importlib.import_module("panel_core.roles.worker").create_app()


def test_a_node_seeds_no_bot_texts_and_publishes_nothing(monkeypatch, tmp_path, published):
    from panel_core.models import BotText, SystemSetting

    app = _build_worker(monkeypatch, tmp_path)

    with app.app_context():
        assert BotText.query.count() == 0, "the node reads no bot text; seeding them only wastes rows"
        assert SystemSetting.query.filter_by(key="bot_texts_seeded_version").first() is None

    assert "texts_changed" not in published, (
        "a node restart must not make the bot re-read its texts: the version it would compare against "
        "lives in that node's local table, which nothing else can see."
    )


def test_a_node_generates_no_bot_service_token(monkeypatch, tmp_path, published):
    from panel_core.models import SystemSetting

    app = _build_worker(monkeypatch, tmp_path)

    with app.app_context():
        assert SystemSetting.query.filter_by(key="bot_service_token").first() is None, (
            "the bot_service blueprint is registered by bot-api alone, so a token minted here could "
            "never be presented to anything — it is a live credential with no purpose."
        )


def test_a_node_still_gets_its_admin_row_and_its_xray_config(monkeypatch, tmp_path, published):
    """The half that must survive the cut."""

    from panel_core.models import Admin, Outbound

    app = _build_worker(monkeypatch, tmp_path)

    with app.app_context():
        assert Admin.query.first() is not None, "a node has its own login; §8.6 calls that expected"
        assert {o.tag for o in Outbound.query.all()} >= {"direct", "block"}


def test_the_master_keeps_the_bot_machinery(monkeypatch, tmp_path):
    """The cut is by role, not global: the master owns the shared Postgres surface the bot reads."""

    from panel_core.models import BotText, SystemSetting
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/master.db"))
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    import importlib

    app = importlib.import_module("panel_core.roles.master").create_app()

    with app.app_context():
        token = SystemSetting.query.filter_by(key="bot_service_token").first()
        assert token is not None and token.value
        assert BotText.query.count() > 0, "the schema the master boots against carries the seeded texts"
