DATA_PLANE = {"sync_traffic", "check_limits", "parse_logs"}
DB_MAINTENANCE = {"cleanup_stats"}
EVENT_BUS = {"cleanup_bot_events", "replay_undelivered_bot_events"}
MASTER_ONLY = {
    "auto_renew_free_users",
    "poll_pending_payments",
    "reconcile_refunds",
    "cleanup_old_payments",
    "poll_linked_panels",
    "check_latest_version",
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
    assert DB_MAINTENANCE.issubset(ids)
    assert ids.isdisjoint(MASTER_ONLY)


def test_master_registers_master_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "master")
    assert MASTER_ONLY.issubset(ids)
    assert DB_MAINTENANCE.issubset(ids)


def test_master_registers_no_data_plane_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "master")
    assert ids.isdisjoint(DATA_PLANE)


def test_worker_registers_event_bus_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "worker")
    assert EVENT_BUS.issubset(ids)


def test_master_still_registers_event_bus_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "master")
    assert EVENT_BUS.issubset(ids)
