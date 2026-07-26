from unittest.mock import patch

import pytest

BOT_API_JOBS = {"poll_pending_payments", "reconcile_refunds", "cleanup_old_payments"}


@pytest.fixture(autouse=True)
def _scheduler_teardown():
    yield

    from panel_core.extensions import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    scheduler.remove_all_jobs()


def _env(monkeypatch, role):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("PANEL_DOMAIN", "localhost")
    monkeypatch.setenv("PROXY_DOMAIN", "localhost")
    monkeypatch.setenv("PANEL_SECRET_PATH", "/test")
    monkeypatch.setenv("PANEL_ADMIN_USER", "admin")
    monkeypatch.setenv("PANEL_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "memory://")


def test_bot_mode_mounts_only_bot_and_billing(monkeypatch):
    _env(monkeypatch, "bot")
    with patch("panel_core.app_base.run_startup_migration") as m_mig:
        from panel_core.dispatch import create_app
        from panel_core.extensions import scheduler

        app = create_app()
        rules = {r.rule for r in app.url_map.iter_rules()}
        jobs = list(scheduler.get_jobs())
        scheduler.remove_all_jobs()

    assert any(r.startswith("/api/bot-service") or r.startswith("/api/bot/") for r in rules)
    assert any(r.startswith("/api/billing") for r in rules)
    assert not any(r.startswith("/api/inbound") for r in rules)
    assert not any(r.startswith("/api/panels") for r in rules)
    assert not any(r.startswith("/api/sub") for r in rules)
    assert "/healthz" in rules and "/readyz" in rules
    assert {job.id for job in jobs} == BOT_API_JOBS
    m_mig.assert_not_called()


def test_bot_mode_gates_yookassa_webhook(monkeypatch):
    _env(monkeypatch, "bot")
    with patch("panel_core.app_base.run_startup_migration"):
        from panel_core.dispatch import create_app
        from panel_core.extensions import scheduler

        app = create_app()
        client = app.test_client()
        resp = client.post("/api/billing/yookassa/webhook", json={"event": "payment.succeeded", "object": {"id": "x"}})
        scheduler.remove_all_jobs()

    assert resp.status_code == 404
