from unittest.mock import patch


def _env(monkeypatch, role):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("PANEL_DOMAIN", "localhost")
    monkeypatch.setenv("PROXY_DOMAIN", "localhost")
    monkeypatch.setenv("PANEL_SECRET_PATH", "/test")
    monkeypatch.setenv("PANEL_ADMIN_USER", "admin")
    monkeypatch.setenv("PANEL_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "memory://")


def test_sub_mode_mounts_only_subscription(monkeypatch):
    _env(monkeypatch, "sub")
    with patch("panel_core.app_base.run_startup_migration") as m_mig:
        from panel_core.dispatch import create_app
        from panel_core.extensions import scheduler

        app = create_app()
        rules = {r.rule for r in app.url_map.iter_rules()}
        jobs = list(scheduler.get_jobs())
        scheduler.remove_all_jobs()

    assert any(r.startswith("/api/sub") for r in rules)
    assert not any(r.startswith("/api/inbound") for r in rules)
    assert not any(r.startswith("/api/billing") for r in rules)
    assert not any(r.startswith("/api/panels") for r in rules)
    assert "/healthz" in rules and "/readyz" in rules
    assert jobs == []
    m_mig.assert_not_called()


def test_master_mode_unchanged(monkeypatch):
    _env(monkeypatch, "master")
    from panel_core.dispatch import create_app
    from panel_core.extensions import scheduler

    app = create_app()
    rules = {r.rule for r in app.url_map.iter_rules()}
    scheduler.remove_all_jobs()
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass
    assert any(r.startswith("/api/inbound") for r in rules)
    assert not any(r.startswith("/api/billing") for r in rules)
    assert not any(r.startswith("/api/sub") for r in rules), (
        "since wave 3b subscriptions are served by the sub role alone. A master answering /api/sub/* "
        "means the blueprint crept back onto an admin host, where it needs no authentication."
    )
