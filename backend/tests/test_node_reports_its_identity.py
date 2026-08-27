def test_cold_state_reports_the_node_identity(app, db, monkeypatch):
    from panel_core.services.state_export import export_cold_state

    monkeypatch.setenv("PANEL_DOMAIN", "alpha.example.com")
    monkeypatch.setenv("PROXY_DOMAIN", "www.google.com")
    monkeypatch.setenv("PANEL_SECRET_PATH", "s3cr3tp4th")

    identity = export_cold_state()["identity"]

    assert identity == {
        "panel_domain": "alpha.example.com",
        "proxy_domain": "www.google.com",
        "secret_path": "s3cr3tp4th",
    }, (
        "PROXY_DOMAIN знает только сама нода. Разойдётся с serverNames в REALITY-инбаунде — "
        "Caddy отдаст весь трафик панели вместо Xray, и никто не подключится, причём молча"
    )


def test_identity_survives_missing_env(app, db, monkeypatch):
    from panel_core.services.state_export import export_cold_state

    for name in ("PANEL_DOMAIN", "PROXY_DOMAIN", "PANEL_SECRET_PATH"):
        monkeypatch.delenv(name, raising=False)

    assert export_cold_state()["identity"] == {"panel_domain": "", "proxy_domain": "", "secret_path": ""}
