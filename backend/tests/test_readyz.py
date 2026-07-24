def test_readyz_ok(app):
    client = app.test_client()
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ready"
