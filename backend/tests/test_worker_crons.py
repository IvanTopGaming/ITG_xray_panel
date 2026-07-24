DATA_PLANE = {"sync_traffic", "check_limits", "parse_logs", "cleanup_stats"}
MASTER_ONLY = {
    "auto_renew_free_users",
    "poll_pending_payments",
    "reconcile_refunds",
    "cleanup_old_payments",
    "cleanup_bot_events",
    "replay_undelivered_bot_events",
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
    from app import create_app
    from app.extensions import scheduler

    create_app()
    ids = {j.id for j in scheduler.get_jobs()}
    scheduler.remove_all_jobs()
    if scheduler.running:
        scheduler.shutdown(wait=False)
    return ids


def test_worker_registers_only_data_plane_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "worker")
    assert DATA_PLANE.issubset(ids)
    assert ids.isdisjoint(MASTER_ONLY)


def test_master_registers_master_crons(monkeypatch):
    ids = _job_ids(monkeypatch, "master")
    assert DATA_PLANE.issubset(ids)
    assert MASTER_ONLY.issubset(ids)
