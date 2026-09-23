import time

import jwt
import pytest
from werkzeug.security import check_password_hash, generate_password_hash

from panel_core.models import Admin, SystemSetting
from panel_core.utils import SECRET_KEY


@pytest.fixture
def api(app, db, monkeypatch):
    from panel_core.api import auth, bot_admin
    from panel_core.extensions import limiter

    monkeypatch.setattr(limiter, "enabled", False)

    app.register_blueprint(auth.bp, url_prefix="/api")
    app.register_blueprint(bot_admin.bp, url_prefix="/api")
    first = Admin(username="first", password=generate_password_hash("FirstPass123"), password_changed_at=1)
    admin = Admin(
        username="second", password=generate_password_hash("CurrentPass123"), password_changed_at=int(time.time()) + 60
    )
    db.session.add_all([first, admin])
    db.session.commit()
    token = jwt.encode(
        {"admin_id": admin.id, "role": "admin", "pwdv": admin.password_changed_at, "exp": int(time.time()) + 3600},
        SECRET_KEY,
        algorithm="HS256",
    )
    return app.test_client(), {"Authorization": f"Bearer {token}"}, admin, first


def test_password_change_targets_authenticated_admin_and_invalidates_token(api, db):
    client, headers, admin, first = api
    response = client.put(
        "/api/admin/password",
        headers=headers,
        json={"current_password": "CurrentPass123", "new_password": "NewPassword123"},
    )
    assert response.status_code == 200
    assert check_password_hash(admin.password, "NewPassword123")
    assert check_password_hash(first.password, "FirstPass123")
    assert client.get("/api/bot/settings", headers=headers).status_code == 401


@pytest.mark.parametrize("password", [None, "wrong", 123, [], {}])
def test_password_change_requires_current_secret(api, password):
    client, headers, admin, _ = api
    response = client.put(
        "/api/admin/password", headers=headers, json={"current_password": password, "new_password": "NewPassword123"}
    )
    assert response.status_code in (400, 403)
    assert check_password_hash(admin.password, "CurrentPass123")


def test_settings_response_can_roundtrip_without_overwriting_secrets(api, db):
    client, headers, _, _ = api
    for key in ("bot_token", "yookassa_secret_key", "bot_service_token"):
        db.session.add(SystemSetting(key=key, value=f"secret-{key}"))
    db.session.commit()
    response = client.get("/api/bot/settings", headers=headers)
    assert response.status_code == 200
    assert "secret-" not in response.text
    assert client.put("/api/bot/settings", headers=headers, json=response.json).status_code == 200
    for key in ("bot_token", "yookassa_secret_key", "bot_service_token"):
        assert SystemSetting.query.filter_by(key=key).one().value == f"secret-{key}"
