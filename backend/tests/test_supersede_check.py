import time
from unittest.mock import patch

import pytest

from panel_core.models import FederationConfig, LinkedPanel, SystemSetting


@pytest.fixture
def app(app):
    from panel_core.api import panels

    if not any(bp_name == "panels" for bp_name in app.blueprints):
        app.register_blueprint(panels.bp, url_prefix="/api")
    return app


@pytest.fixture(autouse=True)
def _reset_check_failure_warned():
    from panel_core.jobs import transfer as job

    job._check_failure_warned.clear()
    yield
    job._check_failure_warned.clear()


def _panel(db, **kw):
    defaults = dict(
        name="alpha", url="https://h/s", federation_token="live-fed", created_at=1, current_instance_id="inst-new"
    )
    defaults.update(kw)
    panel = LinkedPanel(**defaults)
    db.session.add(panel)
    db.session.commit()
    return panel


@pytest.fixture
def linked_master(db, monkeypatch):
    monkeypatch.setenv("PANEL_ROLE", "worker")
    cfg = db.session.get(FederationConfig, 1)
    cfg.master_url = "https://hq.example.com/s"
    cfg.federation_token = "live-fed"
    db.session.commit()
    return cfg


def test_the_live_instance_is_current(app, db):
    from panel_core.services.panel_transfer import instance_verdict

    _panel(db)
    assert instance_verdict("live-fed", "inst-new")["verdict"] == "current"


def test_the_superseded_instance_is_told_so_explicitly(app, db):
    from panel_core.services.panel_transfer import instance_verdict

    _panel(db, superseded_token="dead-fed", superseded_instance_id="inst-old", superseded_at=1_700_000_000_000)

    result = instance_verdict("dead-fed", "inst-old")

    assert result["verdict"] == "superseded", (
        "по одному только токену ответом был бы 401, а он приходит ещё и при отозванном доступе "
        "перед перелинковкой, при удалённой панели и при опечатке в конфиге. Мастер помнит "
        "замещённый токен вместе с экземпляром именно ради недвусмысленного ответа"
    )
    assert result["superseded_at"] == 1_700_000_000_000


def test_a_stranger_gets_unknown_not_superseded(app, db):
    from panel_core.services.panel_transfer import instance_verdict

    _panel(db)
    assert instance_verdict("some-other-token", "inst-whatever")["verdict"] == "unknown"


def test_a_matching_token_with_a_wrong_instance_is_unknown(app, db):
    from panel_core.services.panel_transfer import instance_verdict

    _panel(db, superseded_token="dead-fed", superseded_instance_id="inst-old", superseded_at=1)
    assert instance_verdict("dead-fed", "inst-someone-else")["verdict"] == "unknown"


def test_a_matching_token_with_no_recorded_instance_is_superseded_regardless_of_the_instance_presented(app, db):
    from panel_core.services.panel_transfer import instance_verdict

    _panel(db, superseded_token="dead-fed", superseded_instance_id=None, superseded_at=1)

    result = instance_verdict("dead-fed", "whatever-the-zombie-happens-to-present")

    assert result["verdict"] == "superseded", (
        "claim_transfer — единственный писатель пары (superseded_token, superseded_instance_id); "
        "пустой instance значит, что мастер не смог узнать инстанс зомби (панель заведена до этой "
        "ветки, либо нода умерла до опроса новым кодом), а не что зомби вправе предъявить что угодно"
    )


def test_an_empty_recorded_instance_does_not_wave_through_a_mismatched_token(app, db):
    from panel_core.services.panel_transfer import instance_verdict

    _panel(db, superseded_token="dead-fed", superseded_instance_id=None, superseded_at=1)

    result = instance_verdict("some-other-token", "inst-whatever")

    assert result["verdict"] == "unknown", (
        "ослабление сверки инстанса не должно превращаться в подстановочный знак — токен обязан совпасть в любом случае"
    )


def test_a_live_node_still_gets_current_despite_another_panels_empty_superseded_instance(app, db):
    from panel_core.services.panel_transfer import instance_verdict

    _panel(db, name="alpha")
    _panel(
        db,
        name="bravo",
        federation_token="dead-fed",
        current_instance_id="inst-bravo-live",
        superseded_token="dead-fed",
        superseded_instance_id=None,
        superseded_at=1,
    )

    result = instance_verdict("live-fed", "inst-new")

    assert result["verdict"] == "current", (
        "действующая нода со своим живым токеном обязана получать current независимо от того, что "
        "у другой панели в базе висит замещённая пара с пустым инстансом"
    )


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("timeout"),
        RuntimeError("connection refused"),
        RuntimeError("master answered HTTP 500: (no message)"),
        RuntimeError("master answered HTTP 404: (no message)"),
        RuntimeError("master answered HTTP 401: (no message)"),
        ValueError("Expecting value: line 1 column 1 (char 0)"),
    ],
)
def test_every_failure_leaves_the_node_working(app, db, monkeypatch, linked_master, failure):
    from panel_core.jobs import transfer as job
    from panel_core.services.supersede import is_superseded

    monkeypatch.setenv("NODE_TRANSFER_TOKEN", "")
    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.side_effect = failure
        job.supersede_check_job()

    assert is_superseded() is False, (
        f"глушимся только по явному вердикту. {failure!r} должно означать «работай как обычно»: "
        "защита, которая иногда срабатывает не на том, хуже отсутствия защиты"
    )


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"verdict": "who-knows"},
        {"verdict": None},
        {"verdict": "current"},
        {"verdict": "unknown"},
        "<html>not json at all</html>",
    ],
)
def test_every_non_verdict_reply_leaves_the_node_working(app, db, linked_master, reply):
    from panel_core.jobs import transfer as job
    from panel_core.services.supersede import is_superseded

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.return_value = reply
        job.supersede_check_job()

    assert is_superseded() is False


def test_only_an_explicit_superseded_verdict_stands_the_node_down(app, db, linked_master):
    from panel_core.jobs import transfer as job
    from panel_core.services.supersede import is_superseded

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.return_value = {
            "verdict": "superseded",
            "superseded_at": 1_700_000_000_000,
        }
        job.supersede_check_job()

    assert is_superseded() is True


def test_a_superseded_node_keeps_asking_the_master(app, db, linked_master):
    from panel_core.jobs import transfer as job
    from panel_core.services.supersede import mark_superseded

    mark_superseded(1_700_000_000_000)

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.return_value = {"verdict": "unknown"}
        job.supersede_check_job()

    assert client_cls.return_value.instance_check.called, (
        "старую машину могли вернуть в строй и снова заявить как действующую — если отметка "
        "перестаёт перепроверяться, обратный ход из спеки не сработает никогда"
    )


def test_a_current_verdict_clears_a_stale_superseded_mark(app, db, linked_master):
    from panel_core.jobs import transfer as job
    from panel_core.services.supersede import is_superseded, mark_superseded

    mark_superseded(1_700_000_000_000)

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.return_value = {"verdict": "current"}
        job.supersede_check_job()

    assert is_superseded() is False, (
        "выдать строку переноса ещё раз, старая машина её предъявит и станет действующей на "
        "мастере — это обязано снять отметку и у неё самой, а не только на мастере"
    )


def test_an_ambiguous_verdict_leaves_a_stale_superseded_mark_untouched(app, db, linked_master):
    from panel_core.jobs import transfer as job
    from panel_core.services.supersede import is_superseded, mark_superseded

    mark_superseded(1_700_000_000_000)

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.return_value = {"verdict": "unknown"}
        job.supersede_check_job()

    assert is_superseded() is True, (
        "только явный вердикт current обязан снимать отметку — unknown не доказывает, что нода "
        "снова действующая, оно доказывает только то, что мастер не узнал предъявленную пару"
    )


def test_a_master_outage_leaves_a_stale_superseded_mark_untouched(app, db, linked_master):
    from panel_core.jobs import transfer as job
    from panel_core.services.supersede import is_superseded, mark_superseded

    mark_superseded(1_700_000_000_000)

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.side_effect = RuntimeError("timeout")
        job.supersede_check_job()

    assert is_superseded() is True


def test_instance_check_endpoint_is_unauthenticated_and_answers_the_verdict(app, db):
    _panel(db)

    resp = app.test_client().post(
        "/api/panels/transfer/instance-check",
        json={"federation_token": "live-fed", "instance_id": "inst-new"},
    )

    assert resp.status_code == 200
    assert resp.get_json()["verdict"] == "current"


def test_mark_superseded_persists_the_timestamp(app, db):
    from panel_core.services.supersede import SUPERSEDED_SETTING_KEY, mark_superseded, superseded_at

    mark_superseded(1_700_000_000_000)

    row = db.session.get(SystemSetting, SUPERSEDED_SETTING_KEY)
    assert row is not None
    assert superseded_at() == 1_700_000_000_000


def test_instance_check_endpoint_is_rate_limited(monkeypatch, tmp_path):
    from panel_core.extensions import limiter, scheduler
    from tests.schema import ensure_schema

    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/instance-check-rate.db"))
    monkeypatch.chdir(tmp_path)

    if scheduler.running:
        scheduler.shutdown(wait=False)
    for scheduled in list(scheduler.get_jobs()):
        scheduler.remove_job(scheduled.id)

    from panel_core.dispatch import create_app

    master_app = create_app()
    client = master_app.test_client()

    with master_app.app_context():
        limiter.reset()
    try:
        codes = {
            client.post(
                "/api/panels/transfer/instance-check",
                json={"federation_token": "x", "instance_id": "y"},
            ).status_code
            for _ in range(31)
        }
    finally:
        with master_app.app_context():
            limiter.reset()

    assert 429 in codes, (
        "снятый или забытый декоратор лимита на неаутентифицированной ручке молчит без единого "
        "красного теста — у Limiter в проекте нет default_limits, так что без явного @limiter.limit "
        "защиты нет вовсе, а не «более слабой»"
    )


def test_a_broken_master_check_warns_once_not_every_tick(app, db, linked_master, caplog):
    import logging

    from panel_core.jobs import transfer as job

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.side_effect = RuntimeError("timeout")
        with caplog.at_level(logging.DEBUG):
            job.supersede_check_job()
            job.supersede_check_job()
            job.supersede_check_job()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        "мастер недоступен постоянно на дефолтном INFO — DEBUG в журнал не попадёт вовсе, значит "
        "админ обязан узнать об этом хотя бы раз, а не получать по предупреждению на каждый "
        "пятиминутный тик до конца времён"
    )


def test_a_recovered_check_warns_again_on_the_next_outage(app, db, linked_master, caplog):
    import logging

    from panel_core.jobs import transfer as job

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.side_effect = RuntimeError("timeout")
        job.supersede_check_job()

        client_cls.return_value.instance_check.side_effect = None
        client_cls.return_value.instance_check.return_value = {"verdict": "unknown"}
        job.supersede_check_job()

        caplog.clear()
        client_cls.return_value.instance_check.side_effect = RuntimeError("timeout again")
        with caplog.at_level(logging.DEBUG):
            job.supersede_check_job()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        "новая авария после успешного тика — это новая авария, а не продолжение старой, значит "
        "она заслуживает собственный WARNING"
    )


def test_a_superseded_verdict_with_no_timestamp_records_now_not_the_epoch(app, db, linked_master):
    from panel_core.jobs import transfer as job
    from panel_core.services.supersede import superseded_at

    before = int(time.time() * 1000)
    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.return_value = {"verdict": "superseded", "superseded_at": 0}
        job.supersede_check_job()
    after = int(time.time() * 1000)

    assert before <= superseded_at() <= after, (
        "мастер не прислал внятного времени замещения — плашка обязана показать «сейчас», а не 1970 год"
    )


def test_a_garbage_superseded_at_does_not_crash_the_job(app, db, linked_master):
    from panel_core.jobs import transfer as job
    from panel_core.services.supersede import is_superseded

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.return_value = {
            "verdict": "superseded",
            "superseded_at": "not-a-number",
        }
        job.supersede_check_job()

    assert is_superseded() is True, (
        "нечисловой superseded_at не должен ронять джобу целиком — вердикт мастера всё ещё "
        "однозначный, только время неразборчиво"
    )


def test_a_repeated_superseded_verdict_does_not_rewrite_the_timestamp(app, db, linked_master):
    from panel_core.jobs import transfer as job
    from panel_core.services.supersede import superseded_at

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.instance_check.return_value = {
            "verdict": "superseded",
            "superseded_at": 1_700_000_000_000,
        }
        job.supersede_check_job()

        client_cls.return_value.instance_check.return_value = {"verdict": "superseded", "superseded_at": 0}
        job.supersede_check_job()
        job.supersede_check_job()

    assert superseded_at() == 1_700_000_000_000, (
        "мастер подтверждает superseded на каждом тике, а не только один раз при переходе — "
        "перезаписывать дату замещения при каждом подтверждении значит, что плашка Task 13 "
        "навсегда будет показывать «только что», даже если замена случилась месяц назад"
    )
