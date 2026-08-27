from unittest.mock import MagicMock, patch

from panel_core.models import LinkedPanel


def _panel(db):
    panel = LinkedPanel(name="alpha", url="https://n/x", federation_token="t", created_at=1)
    db.session.add(panel)
    db.session.commit()
    return panel


def _snapshot(fingerprint="a" * 64, clients=2):
    return {
        "app_version": "3.2.0",
        "status": "ok",
        "timestamp": 1_700_000_000_000,
        "instance_id": "inst-1",
        "cold_fingerprint": fingerprint,
        "inbounds": [
            {
                "tag": "in-1",
                "port": 443,
                "protocol": "vless",
                "stream_settings": {},
                "clients": [{"id": f"uuid-{i}"} for i in range(clients)],
            }
        ],
    }


def test_poll_linked_panels_writes_the_mirror_through_the_real_greenlet_pool(app, db):
    from panel_core.jobs import panels as job
    from panel_core.services.state_mirror import read_current

    panel = _panel(db)

    with (
        patch.object(job, "_fetch_cold", return_value={"cold": {}, "fingerprint": "a" * 64}),
        patch.object(job, "FederationClient") as client_cls,
        patch("panel_core.services.panel_proxy.get_shared_redis", return_value=MagicMock()),
    ):
        client_cls.return_value.snapshot.return_value = _snapshot()
        job.poll_linked_panels()

    db.session.refresh(panel)
    assert panel.status == "online", panel.last_error
    assert read_current(panel.id) is not None, (
        "опрос идёт через настоящий gevent.pool: если зеркалирование в дочернем гринлете осталось "
        "без контекста приложения, запись в Postgres молча не происходит, хотя сам опрос выглядит успешным"
    )


def test_cold_state_is_fetched_only_when_the_fingerprint_moves(app, db):
    from panel_core.jobs import panels as job

    panel = _panel(db)
    cold_calls = []

    def fake_state():
        cold_calls.append(1)
        return {"cold": {"outbounds": []}, "fingerprint": "a" * 64}

    with patch.object(job, "_fetch_cold", side_effect=lambda *a, **k: fake_state()):
        job.mirror_from_snapshot(panel.id, _snapshot())
        job.mirror_from_snapshot(panel.id, _snapshot())
        job.mirror_from_snapshot(panel.id, _snapshot(fingerprint="b" * 64))

    assert len(cold_calls) == 2, (
        "второй опрос с тем же отпечатком не должен тянуть холодную половину — иначе мы возим "
        "200 КБ каждые 10 секунд ради данных, которые меняются раз в месяц"
    )


def test_cold_state_is_refetched_after_fifteen_minutes_even_with_the_same_fingerprint(app, db):
    from panel_core.jobs import panels as job

    panel = _panel(db)
    cold_calls = []

    def fake_state():
        cold_calls.append(1)
        return {"cold": {"outbounds": []}, "fingerprint": "a" * 64}

    with patch.object(job, "_fetch_cold", side_effect=lambda *a, **k: fake_state()):
        job.mirror_from_snapshot(panel.id, _snapshot())
        stale = _snapshot()
        stale["timestamp"] += 16 * 60 * 1000
        job.mirror_from_snapshot(panel.id, stale)

    assert len(cold_calls) == 2, (
        "отпечаток — не единственная страховка: если он застрянет на стороне ноды, холодная половина "
        "не должна замереть навсегда — раз в 15 минут она обновляется независимо от отпечатка"
    )


def test_cold_fetch_does_not_hold_an_open_transaction_during_the_http_call(app, db):
    from panel_core.jobs import panels as job
    from panel_core.extensions import db as _db

    panel = _panel(db)
    observed = {}

    def fake_fetch(url, token):
        observed["in_transaction"] = _db.session().in_transaction()
        return {"cold": {}, "fingerprint": "a" * 64}

    with patch.object(job, "_fetch_cold", side_effect=fake_fetch):
        job.mirror_from_snapshot(panel.id, _snapshot())

    assert observed["in_transaction"] is False, (
        "холодная половина тянется по HTTP уже после того, как write_hot закоммитил свою транзакцию — "
        "обращение к истёкшему ORM-объекту после коммита переоткрывает новую и держит соединение в "
        "простое весь таймаут запроса"
    )


def test_a_failed_poll_never_blanks_the_mirror(app, db):
    from panel_core.jobs import panels as job
    from panel_core.services.state_mirror import read_current

    panel = _panel(db)
    with patch.object(job, "_fetch_cold", return_value={"cold": {}, "fingerprint": "a" * 64}):
        job.mirror_from_snapshot(panel.id, _snapshot())

    before = read_current(panel.id).hot_state

    with patch.object(job, "FederationClient") as client_cls:
        client_cls.return_value.snapshot.side_effect = RuntimeError("unreachable")
        job._poll_one(panel.id, panel.url, panel.federation_token)

    assert read_current(panel.id).hot_state == before, (
        "отсутствие ответа означает «не знаю», а не «стало пусто». Затирать копию из-за того, что "
        "сервер не ответил, — самый быстрый способ остаться без неё"
    )


def test_a_malformed_snapshot_is_refused_whole(app, db):
    from panel_core.jobs import panels as job
    from panel_core.services.state_mirror import read_current

    panel = _panel(db)
    with patch.object(job, "_fetch_cold", return_value={"cold": {}, "fingerprint": "a" * 64}):
        job.mirror_from_snapshot(panel.id, _snapshot())
    before = read_current(panel.id).hot_state

    job.mirror_from_snapshot(panel.id, {"inbounds": "не список"})

    assert read_current(panel.id).hot_state == before, (
        "разложить половину состояния — значит создать копию, которая выглядит целой и не является"
    )


def test_a_halved_client_count_writes_but_raises_the_flag(app, db):
    from panel_core.jobs import panels as job
    from panel_core.services.state_mirror import read_current

    panel = _panel(db)
    with patch.object(job, "_fetch_cold", return_value={"cold": {}, "fingerprint": "a" * 64}):
        job.mirror_from_snapshot(panel.id, _snapshot(clients=10))
        job.mirror_from_snapshot(panel.id, _snapshot(clients=2))

    row = read_current(panel.id)
    assert row.shrink_flagged is True
    assert '"uuid-1"' in row.hot_state, (
        "флаг информирует, а не блокирует: законное удаление инбаунда тоже роняет счётчик, "
        "а страховкой служат суточные копии"
    )


def test_a_mirror_write_failure_does_not_break_the_poll(app, db):
    from panel_core.jobs import panels as job

    panel = _panel(db)
    with patch.object(job, "mirror_from_snapshot", side_effect=RuntimeError("postgres down")):
        with patch.object(job, "FederationClient") as client_cls:
            client_cls.return_value.snapshot.return_value = _snapshot()
            result = job._poll_one(panel.id, panel.url, panel.federation_token)

    assert client_cls.return_value.snapshot.called
    assert result[1] == "online", (
        "опросом кормятся подписки: если он начнёт падать из-за зеркала, мы поменяем «нет "
        "резервной копии» на «у людей не обновляются подписки». Плохой размен"
    )


def test_a_broken_rollback_after_mirror_failure_does_not_break_the_poll(app, db):
    from panel_core.jobs import panels as job

    panel = _panel(db)
    with (
        patch.object(job, "mirror_from_snapshot", side_effect=RuntimeError("postgres down")),
        patch.object(job.db.session, "rollback", side_effect=RuntimeError("connection already closed")),
        patch.object(job, "FederationClient") as client_cls,
    ):
        client_cls.return_value.snapshot.return_value = _snapshot()
        result = job._poll_one(panel.id, panel.url, panel.federation_token)

    assert result[1] == "online", (
        "rollback() сам может упасть тем же способом, что и запись — вторая линия защиты обязана "
        "проглотить и эту ошибку, а не отдать её во внешний except, который гасит ноду"
    )


def test_daily_archive_keeps_seven_days(app, db):
    from panel_core.jobs import panels as job
    from panel_core.models import PanelStateMirror

    panel = _panel(db)
    with patch.object(job, "_fetch_cold", return_value={"cold": {}, "fingerprint": "a" * 64}):
        job.mirror_from_snapshot(panel.id, _snapshot())

    job.archive_panel_state()

    assert PanelStateMirror.query.filter_by(panel_id=panel.id, kind="daily").count() == 1


def test_archive_panel_state_isolates_one_panels_failure_from_the_rest(app, db):
    from panel_core.jobs import panels as job

    panel_a = _panel(db)
    panel_b = LinkedPanel(name="b", url="https://n/y", federation_token="t2", created_at=1)
    db.session.add(panel_b)
    db.session.commit()

    archived = []

    def flaky_archive_daily(panel_id, *, taken_at):
        if panel_id == panel_a.id:
            raise RuntimeError("boom")
        archived.append(panel_id)

    pruned = []

    def fake_prune_archive(*, older_than_ms):
        pruned.append(older_than_ms)
        return 0

    with (
        patch("panel_core.services.state_mirror.archive_daily", side_effect=flaky_archive_daily),
        patch("panel_core.services.state_mirror.prune_archive", side_effect=fake_prune_archive),
    ):
        job.archive_panel_state()

    assert archived == [panel_b.id], "падение archive_daily для одной панели не должно останавливать цикл по остальным"
    assert pruned, (
        "падение на одной панели не должно отменять prune_archive — иначе уборка архива перестаёт "
        "выполняться из-за одной битой строки"
    )


def test_archive_panel_state_clears_a_transfer_token_a_day_past_its_expiry(app, db):
    from panel_core.jobs import panels as job

    panel = _panel(db)
    panel.transfer_token = "stale-secret"
    panel.transfer_token_expires_at = 1_700_000_000_000
    db.session.commit()

    with patch("panel_core.jobs.panels.time.time", return_value=(1_700_000_000_000 + 25 * 3_600_000) / 1000):
        job.archive_panel_state()

    fresh = db.session.get(LinkedPanel, panel.id)
    assert fresh.transfer_token is None, (
        "просроченный ключ переноса не должен пережидать сутки в базе и в суточных дампах pg-backup"
    )


def test_archive_panel_state_keeps_a_transfer_token_inside_its_grace_window(app, db):
    from panel_core.jobs import panels as job

    panel = _panel(db)
    panel.transfer_token = "fresh-secret"
    panel.transfer_token_expires_at = 1_700_000_000_000
    db.session.commit()

    with patch("panel_core.jobs.panels.time.time", return_value=(1_700_000_000_000 + 3_600_000) / 1000):
        job.archive_panel_state()

    fresh = db.session.get(LinkedPanel, panel.id)
    assert fresh.transfer_token == "fresh-secret"
