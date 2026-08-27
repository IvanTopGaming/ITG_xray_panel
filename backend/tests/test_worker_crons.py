DATA_PLANE = {"sync_traffic", "check_limits", "parse_logs"}
WORKER_ONLY_MAINTENANCE = {"cleanup_stats"}
EVENT_BUS = {"cleanup_bot_events", "replay_undelivered_bot_events"}
CRON_ONLY = {
    "reset_grant_traffic_cycles",
    "poll_linked_panels",
    "check_latest_version",
    "archive_panel_state",
}
BOT_API_ONLY = {
    "poll_pending_payments",
    "reconcile_refunds",
    "cleanup_old_payments",
}


def _job_ids(monkeypatch, role):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("PANEL_DOMAIN", "localhost")
    monkeypatch.setenv("PROXY_DOMAIN", "localhost")
    monkeypatch.setenv("PANEL_SECRET_PATH", "/test")
    monkeypatch.setenv("PANEL_ADMIN_USER", "admin")
    monkeypatch.setenv("PANEL_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "memory://")
    from panel_core.dispatch import create_app
    from panel_core.extensions import scheduler

    create_app()
    ids = {j.id for j in scheduler.get_jobs()}
    scheduler.remove_all_jobs()
    if scheduler.running:
        scheduler.shutdown(wait=False)
    return ids


def test_worker_registers_only_data_plane_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "worker")
    assert DATA_PLANE.issubset(ids)
    assert WORKER_ONLY_MAINTENANCE.issubset(ids)
    assert ids.isdisjoint(CRON_ONLY)
    assert ids.isdisjoint(BOT_API_ONLY)


def test_master_registers_no_crons_at_all(monkeypatch):
    ids = _job_ids(monkeypatch, "master")
    assert ids == set()


def test_cron_service_registers_the_jobs_that_left_the_master(monkeypatch):
    ids = _job_ids(monkeypatch, "cron")
    assert ids == CRON_ONLY | EVENT_BUS
    assert ids.isdisjoint(DATA_PLANE)
    assert ids.isdisjoint(WORKER_ONLY_MAINTENANCE)
    assert ids.isdisjoint(BOT_API_ONLY)


def test_master_does_not_register_payment_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "master")
    assert ids.isdisjoint(BOT_API_ONLY)


def test_botapi_registers_payment_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "bot")
    assert ids == BOT_API_ONLY


def test_master_registers_no_data_plane_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "master")
    assert ids.isdisjoint(DATA_PLANE)


def test_worker_registers_event_bus_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "worker")
    assert EVENT_BUS.issubset(ids)


def test_the_node_keeps_its_own_event_bus_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "worker")
    assert EVENT_BUS.issubset(ids), (
        "a node keeps its own replay/cleanup crons because they work on its LOCAL SQLite, which the central "
        "cron service cannot reach. Wave 2 took these jobs off the master, it did not centralise them."
    )
