from unittest.mock import patch


def test_proxy_returns_503_without_admin_url(app, monkeypatch):
    monkeypatch.delenv("ADMIN_BACKEND_URL", raising=False)
    from panel_core.services.admin_proxy import proxy_to_admin

    with app.test_request_context("/api/bot-service/trial/activate", method="POST", json={"telegram_id": 1}):
        body, status = proxy_to_admin("/api/bot-service/trial/activate")
    assert status == 503
    assert "unavailable" in body["error"]


def test_proxy_forwards_and_returns_upstream(app, monkeypatch):
    monkeypatch.setenv("ADMIN_BACKEND_URL", "http://admin:5000")
    from panel_core.services import admin_proxy

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    with app.test_request_context(
        "/api/bot-service/trial/activate", method="POST", json={"telegram_id": 1}, headers={"Authorization": "Bearer t"}
    ):
        with patch.object(admin_proxy.requests, "request", return_value=_Resp()) as m:
            body, status = admin_proxy.proxy_to_admin("/api/bot-service/trial/activate")
    assert status == 200
    assert body == {"ok": True}
    assert m.call_args.kwargs["url"] == "http://admin:5000/api/bot-service/trial/activate"


def test_proxy_returns_503_on_connection_error(app, monkeypatch):
    monkeypatch.setenv("ADMIN_BACKEND_URL", "http://admin:5000")
    from panel_core.services import admin_proxy

    with app.test_request_context("/api/bot-service/trial/activate", method="POST", json={"telegram_id": 1}):
        with patch.object(admin_proxy.requests, "request", side_effect=admin_proxy.requests.RequestException("down")):
            body, status = admin_proxy.proxy_to_admin("/api/bot-service/trial/activate")
    assert status == 503
