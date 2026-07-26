def _rules(monkeypatch, role):
    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("PANEL_DOMAIN", "localhost")
    monkeypatch.setenv("PROXY_DOMAIN", "localhost")
    monkeypatch.setenv("PANEL_SECRET_PATH", "/test")
    monkeypatch.setenv("PANEL_ADMIN_USER", "admin")
    monkeypatch.setenv("PANEL_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "memory://")
    from panel_core.dispatch import create_app

    app = create_app()
    from panel_core.extensions import scheduler

    scheduler.remove_all_jobs()
    rules = {r.rule for r in app.url_map.iter_rules()}
    scheduler.shutdown(wait=False)
    return rules


def _has_prefix(rules, prefix):
    return any(r.startswith(prefix) for r in rules)


def test_worker_drops_master_blueprints(monkeypatch):
    rules = _rules(monkeypatch, "worker")
    assert not _has_prefix(rules, "/api/bot/")
    assert not _has_prefix(rules, "/api/bot-service/")
    assert not _has_prefix(rules, "/api/billing/")
    assert not _has_prefix(rules, "/api/panels")
    assert _has_prefix(rules, "/api/inbound")
    assert _has_prefix(rules, "/api/federation") or _has_prefix(rules, "/api/fed")


def test_master_keeps_all_blueprints(monkeypatch):
    rules = _rules(monkeypatch, "master")
    assert _has_prefix(rules, "/api/billing/")
    assert _has_prefix(rules, "/api/panels")
