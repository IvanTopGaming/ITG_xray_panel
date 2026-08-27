import datetime as dt
import json
import logging
import re
import time
from unittest.mock import MagicMock, patch

from panel_core.models import BotEvent


def _aged(seconds_old: int) -> dt.datetime:
    return dt.datetime.utcnow() - dt.timedelta(seconds=seconds_old)


def test_a_normal_node_publishes(app, db):
    from panel_core.services import bot_events

    redis = MagicMock()
    with patch.object(bot_events, "_get_redis", return_value=redis):
        bot_events.publish("traffic_warning", 777, {"percent": 80})

    assert redis.publish.called, (
        "положительный контроль обязателен: сломанный отправитель выглядит ровно так же, "
        "как заглушённый, и тест без него зеленеет на сломанном коде"
    )
    assert BotEvent.query.count() == 1
    assert BotEvent.query.one().delivered_at is not None


def test_a_superseded_node_writes_the_row_but_stays_silent(app, db, monkeypatch):
    from panel_core.services import bot_events
    from panel_core.services.supersede import mark_superseded

    monkeypatch.setenv("PANEL_ROLE", "worker")
    mark_superseded(1_700_000_000_000)

    redis = MagicMock()
    with patch.object(bot_events, "_get_redis", return_value=redis):
        bot_events.publish("traffic_warning", 777, {"percent": 80})

    assert redis.publish.called is False, (
        "воскресшая нода перепубликовала бы волну «трафик 80 процентов» по состоянию на момент "
        "аварии: реквизиты общего Redis лежат у неё в .env и никуда не делись"
    )
    assert BotEvent.query.count() == 1
    assert BotEvent.query.one().delivered_at is None


def test_a_superseded_node_does_not_replay(app, db, monkeypatch):
    from panel_core.jobs import notifications
    from panel_core.services.supersede import mark_superseded

    monkeypatch.setenv("PANEL_ROLE", "worker")

    event = BotEvent(type="traffic_warning", telegram_id=777, payload={})
    db.session.add(event)
    db.session.flush()
    event.created_at = _aged(120)
    db.session.commit()
    mark_superseded(1_700_000_000_000)

    redis = MagicMock()
    with patch("panel_core.jobs.notifications._get_redis", return_value=redis):
        notifications.replay_undelivered_bot_events()

    assert redis.publish.called is False, (
        "без отметки замещения эта же необновлённая старая запись ушла бы в общий Redis: "
        "проверка обязана перехватывать реальный кандидат на перепубликацию, а не пустой список"
    )


def test_check_limits_still_disables_but_the_notification_stays_muted_when_superseded(app, db, monkeypatch):
    from panel_core.models import Client, Inbound
    from panel_core.services import bot_events
    from panel_core.services.stats import check_limits_and_reset
    from panel_core.services.supersede import mark_superseded

    monkeypatch.setenv("PANEL_ROLE", "worker")
    mark_superseded(1_700_000_000_000)

    db.session.add(Inbound(tag="DE-vless", protocol="vless", port=10001, stream_settings="{}"))
    now_ms = int(time.time() * 1000)
    client = Client(
        id="cli-superseded-exp",
        email="e-superseded",
        inbound_tag="DE-vless",
        telegram_id=999,
        limit_bytes=0,
        up=0,
        down=0,
        enable=True,
        expiry_time=now_ms - 10_000,
        reset_day=0,
    )
    db.session.add(client)
    db.session.commit()

    redis = MagicMock()
    with (
        patch("panel_core.services.stats.get_channel", return_value=MagicMock()),
        patch("panel_core.services.stats._api_remove_user_grpc", return_value=True),
        patch("panel_core.services.stats.generate_config_file"),
        patch("panel_core.services.stats.restart_xray_container"),
        patch.object(bot_events, "_get_redis", return_value=redis),
    ):
        check_limits_and_reset()

    db.session.expire_all()
    assert db.session.get(Client, "cli-superseded-exp").enable is False, (
        "трафик остаётся: просроченный клиент обязан быть отключён независимо от замещения — "
        "иначе застрявший на старом адресе получает фактическую бессрочность"
    )
    event = BotEvent.query.filter_by(type="expiry_notification").one()
    assert event.payload["kind"] == "expired"
    assert event.delivered_at is None, (
        "голос замолкает: строка про истечение обязана остаться недоставленной, а не уйти "
        "наружу от лица ноды, которая больше не эта панель"
    )
    assert redis.publish.called is False, (
        "если это красное — значит кто-то дописал собственную проверку is_superseded() прямо в "
        "check_limits_and_reset() и досрочно вышел из неё, отключив заодно и отключение клиентов"
    )


def test_is_superseded_short_circuits_off_the_worker_role_without_a_db_lookup(app, db, monkeypatch):
    from panel_core.extensions import db as _db
    from panel_core.services.supersede import is_superseded, mark_superseded

    monkeypatch.setenv("PANEL_ROLE", "worker")
    mark_superseded(1_700_000_000_000)

    monkeypatch.setenv("PANEL_ROLE", "master")
    with patch.object(_db.session, "get") as mock_get:
        result = is_superseded()

    assert result is False, (
        "чужая роль не обязана верить чужой отметке — cron, master, sub, bot-api физически "
        "никогда её не пишут, поэтому единственно верный ответ вне worker — False"
    )
    assert not mock_get.called, (
        "цена обязана быть нулевой именно за счёт отсутствия похода в базу — иначе на cron это "
        "по-прежнему 1440 SELECT в сутки за строкой, которой там никогда не будет"
    )


def test_clearing_supersede_off_the_worker_role_touches_nothing(app, db, monkeypatch):
    from panel_core.extensions import db as _db
    from panel_core.services.supersede import clear_superseded, mark_superseded

    monkeypatch.setenv("PANEL_ROLE", "worker")
    mark_superseded(1_700_000_000_000)

    monkeypatch.setenv("PANEL_ROLE", "master")
    with patch.object(_db.session, "get") as mock_get:
        clear_superseded()

    assert not mock_get.called, (
        "запись обязана быть под тем же гейтом, что и чтение — иначе invisible-сегодня bulk-UPDATE "
        "по общему Postgres на cron однажды погасит недоставленные уведомления всему флоту разом"
    )


def test_clearing_supersede_measures_the_window_by_the_nodes_own_clock(app, db, monkeypatch):
    from panel_core.services.supersede import clear_superseded, mark_superseded

    monkeypatch.setenv("PANEL_ROLE", "worker")

    master_reported_ms = int(time.time() * 1000) + 3_600_000
    mark_superseded(master_reported_ms)

    zombie_event = BotEvent(type="traffic_warning", telegram_id=3, payload={})
    db.session.add(zombie_event)
    db.session.commit()
    zombie_event_id = zombie_event.id

    clear_superseded()

    assert db.session.get(BotEvent, zombie_event_id).delivered_at is not None, (
        "мастер прислал время на час вперёд своих часов — граница гашения обязана идти по "
        "локальным часам ноды на момент отметки, а не по чужому значению: иначе рассинхрон часов "
        "оставляет зомби-хвост неопознанным, и он уходит в общий Redis при ближайшем реплее"
    )


def test_clearing_supersede_logs_how_many_rows_it_silenced(app, db, monkeypatch, caplog):
    from panel_core.services.supersede import clear_superseded, mark_superseded

    monkeypatch.setenv("PANEL_ROLE", "worker")
    mark_superseded(1_700_000_000_000)

    db.session.add(BotEvent(type="traffic_warning", telegram_id=10, payload={}))
    db.session.add(BotEvent(type="traffic_warning", telegram_id=11, payload={}))
    db.session.commit()

    with caplog.at_level(logging.INFO):
        clear_superseded()

    matches = [
        m
        for m in (
            re.search(r"silenced (\d+)", r.getMessage())
            for r in caplog.records
            if r.name == "panel_core.services.supersede"
        )
        if m
    ]
    assert matches, (
        "погашенная пачка обязана оставить след в логе — иначе выброшенные уведомления невидимы, "
        "и в разборе полётов их не найти"
    )
    assert matches[0].group(1) == "2", (
        "число в логе обязано быть настоящим счётчиком погашенных строк, а не произвольным текстом"
    )


def test_clearing_supersede_silences_the_backlog_from_that_window_but_not_before(app, db, monkeypatch):
    from panel_core.jobs import notifications
    from panel_core.services.supersede import clear_superseded, mark_superseded

    monkeypatch.setenv("PANEL_ROLE", "worker")

    stale_before = BotEvent(type="payment_succeeded", telegram_id=1, payload={})
    db.session.add(stale_before)
    db.session.flush()
    stale_before.created_at = _aged(200)
    db.session.commit()
    stale_before_id = stale_before.id

    mark_superseded(1_700_000_000_000)

    zombie_event = BotEvent(type="traffic_warning", telegram_id=2, payload={})
    db.session.add(zombie_event)
    db.session.commit()
    zombie_event_id = zombie_event.id

    clear_superseded()

    assert db.session.get(BotEvent, zombie_event_id).delivered_at is not None, (
        "событие, порождённое за время замещения, обязано быть погашено сразу при снятии отметки — "
        "оно уже устарело и дублирует то, что настоящая нода давно отправила сама"
    )
    assert db.session.get(BotEvent, stale_before_id).delivered_at is None, (
        "событие, зависшее ДО замещения, к этому случаю не относится и обязано дождаться обычного "
        "реплея — иначе честно недоставленное уведомление потеряется вместе с зомби-мусором"
    )

    redis = MagicMock()
    with patch("panel_core.jobs.notifications._get_redis", return_value=redis):
        notifications.replay_undelivered_bot_events()

    assert redis.publish.call_count == 1, (
        "реплей обязан подхватить только честно недоставленное — то, что уже погашено при снятии "
        "отметки, публиковаться не должно"
    )
    published = json.loads(redis.publish.call_args.args[1])
    assert published["id"] == stale_before_id, (
        "единственная публикация обязана быть за долив-до-замещения событие, а не за зомби-запись"
    )
