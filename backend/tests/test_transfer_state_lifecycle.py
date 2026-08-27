from unittest.mock import patch

from panel_core.models import LinkedPanel


def _panel(db, transfer_state="awaiting_dns"):
    panel = LinkedPanel(
        name="alpha",
        url="https://alpha.example.com/s",
        federation_token="new-fed",
        created_at=1,
        transfer_state=transfer_state,
        status="online",
    )
    db.session.add(panel)
    db.session.commit()
    return panel


def test_a_failed_poll_during_transfer_does_not_read_as_an_outage(app, db):
    from panel_core.jobs import panels as job

    panel = _panel(db)
    with patch.object(job, "FederationClient") as client_cls:
        client_cls.return_value.snapshot.side_effect = RuntimeError("HTTP 401")
        job._record([job._poll_one(panel.id, panel.url, panel.federation_token)])

    assert panel.status == "transferring", (
        "мастер ходит по домену, который пока указывает на старую машину с мёртвым токеном. "
        "Состояние нормальное и длится ровно до переезда A-записи, но выглядит как авария"
    )
    assert panel.transfer_state == "awaiting_dns"


def test_the_first_successful_poll_clears_the_transfer_state(app, db):
    from panel_core.jobs import panels as job

    panel = _panel(db)
    with patch.object(job, "mirror_from_snapshot"):
        with patch.object(job, "FederationClient") as client_cls:
            client_cls.return_value.snapshot.return_value = {"timestamp": 1, "inbounds": []}
            job._record([job._poll_one(panel.id, panel.url, panel.federation_token)])

    assert panel.transfer_state == ""
    assert panel.status == "online"


def test_a_failed_poll_outside_a_transfer_is_still_an_outage(app, db):
    from panel_core.jobs import panels as job

    panel = _panel(db, transfer_state="")
    with patch.object(job, "FederationClient") as client_cls:
        client_cls.return_value.snapshot.side_effect = RuntimeError("unreachable")
        job._record([job._poll_one(panel.id, panel.url, panel.federation_token)])

    assert panel.status == "offline"


def test_a_transfer_that_never_produced_a_live_node_eventually_clears_state(app, db):
    from panel_core.jobs import panels as job

    panel = _panel(db)
    panel.transfer_token = "stale-secret"
    panel.transfer_token_expires_at = 1_700_000_000_000
    db.session.commit()

    with patch("panel_core.jobs.panels.time.time", return_value=(1_700_000_000_000 + 25 * 3_600_000) / 1000):
        job.archive_panel_state()

    fresh = db.session.get(LinkedPanel, panel.id)
    assert fresh.transfer_state == "", (
        "заявка на перенос, за которой так и не встала живая нода, не должна вечно прятать "
        "настоящую аварию под видом планового переезда"
    )


def test_a_fresh_claim_keeps_awaiting_dns_inside_the_grace_window(app, db):
    from panel_core.jobs import panels as job

    panel = _panel(db)
    panel.transfer_token = "fresh-secret"
    panel.transfer_token_expires_at = 1_700_000_000_000
    db.session.commit()

    with patch("panel_core.jobs.panels.time.time", return_value=(1_700_000_000_000 + 3_600_000) / 1000):
        job.archive_panel_state()

    fresh = db.session.get(LinkedPanel, panel.id)
    assert fresh.transfer_state == "awaiting_dns"


def test_transfer_finished_is_logged_only_after_the_commit_succeeds(app, db, caplog):
    import logging

    from panel_core.jobs import panels as job

    panel = _panel(db)
    with patch.object(job, "mirror_from_snapshot"):
        with patch.object(job, "FederationClient") as client_cls:
            client_cls.return_value.snapshot.return_value = {"timestamp": 1, "inbounds": []}
            result = job._poll_one(panel.id, panel.url, panel.federation_token)

    with patch.object(job.db.session, "commit", side_effect=RuntimeError("db is gone")):
        with caplog.at_level(logging.INFO):
            try:
                job._record([result])
            except RuntimeError:
                pass

    assert not [r for r in caplog.records if "transfer finished" in r.getMessage()], (
        "коммит упал, а строка про завершение переноса уже была бы в журнале — вместо факта "
        "лог отражает намерение, которое не реализовалось"
    )
