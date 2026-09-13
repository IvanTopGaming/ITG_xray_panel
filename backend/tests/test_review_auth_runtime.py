from panel_core.extensions import db, limiter
from panel_core.models import Client, FederationConfig, Inbound, Outbound, RuntimeApplyState


def test_repeating_saved_routing_change_recovers_runtime(app, monkeypatch, tmp_path):
    from panel_core.api import auth
    from panel_core.services import runtime_apply

    app.config.update(RATELIMIT_ENABLED=False, XRAY_CONFIG_LOCK_PATH=str(tmp_path / "config.lock"))
    limiter.init_app(app)
    app.register_blueprint(auth.bp, url_prefix="/api")
    db.session.get(FederationConfig, 1).federation_token = "test-federation"
    db.session.add(Inbound(tag="vless", port=443, protocol="vless", stream_settings="{}"))
    db.session.add(Outbound(tag="edge", protocol="freedom", settings="{}"))
    db.session.add(Client(id="user-id", email="alice", inbound_tag="vless", enable=True))
    db.session.commit()
    generated = []
    restarted = []

    def generate(*args, **kwargs):
        generated.append(kwargs)

    def restart():
        restarted.append(True)
        if len(restarted) == 1:
            raise RuntimeError("injected runtime failure")

    monkeypatch.setattr(auth, "has_local_xray", lambda: True)
    monkeypatch.setattr(runtime_apply, "has_local_xray", lambda: True)
    monkeypatch.setattr(runtime_apply, "generate_config_file", generate)
    monkeypatch.setattr(runtime_apply, "restart_xray_container", restart)
    monkeypatch.setattr(auth, "generate_config_file", generate, raising=False)
    monkeypatch.setattr(auth, "restart_xray_container", restart, raising=False)
    headers = {"X-Federation-Token": "test-federation"}
    payload = {"email": "alice", "inbound_tag": "vless", "outbound_tag": "edge"}
    client = app.test_client()
    first = client.post("/api/user/routing", json=payload, headers=headers)
    assert first.status_code == 503
    assert first.json["saved"] is True
    db.session.expire_all()
    assert db.session.get(Client, "user-id").preferred_outbound == "edge"
    state = db.session.get(RuntimeApplyState, 1)
    assert state.desired_revision > state.applied_revision
    second = client.post("/api/user/routing", json=payload, headers=headers)
    assert second.status_code == 200
    db.session.expire_all()
    state = db.session.get(RuntimeApplyState, 1)
    assert state.desired_revision == state.applied_revision
    assert len(restarted) == 2
    assert generated[0] == {"publish": False}
