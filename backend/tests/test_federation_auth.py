import time
import pytest
import jwt as jwt_lib
from panel_core.models import FederationConfig

from panel_core.utils import SECRET_KEY


@pytest.fixture
def app_with_fed_endpoints(app, db):
    from flask import jsonify
    from panel_core.utils import federation_token_required, admin_or_federation_token_required

    @app.route("/test-fed-only")
    @federation_token_required
    def _fed():
        return jsonify({"ok": True})

    @app.route("/test-admin-or-fed")
    @admin_or_federation_token_required
    def _dual():
        return jsonify({"ok": True})

    cfg = db.session.get(FederationConfig, 1)
    cfg.federation_token = "valid-fed-token-123"
    db.session.commit()
    return app


def test_federation_token_valid(app_with_fed_endpoints):
    c = app_with_fed_endpoints.test_client()
    resp = c.get("/test-fed-only", headers={"X-Federation-Token": "valid-fed-token-123"})
    assert resp.status_code == 200


def test_federation_token_missing(app_with_fed_endpoints):
    c = app_with_fed_endpoints.test_client()
    resp = c.get("/test-fed-only")
    assert resp.status_code == 401


def test_federation_token_wrong(app_with_fed_endpoints):
    c = app_with_fed_endpoints.test_client()
    resp = c.get("/test-fed-only", headers={"X-Federation-Token": "wrong"})
    assert resp.status_code == 401


def test_admin_or_fed_accepts_federation(app_with_fed_endpoints):
    c = app_with_fed_endpoints.test_client()
    resp = c.get("/test-admin-or-fed", headers={"X-Federation-Token": "valid-fed-token-123"})
    assert resp.status_code == 200


def test_admin_or_fed_accepts_jwt(app_with_fed_endpoints, db):
    from panel_core.models import Admin

    admin = Admin(username="admin", password="x", password_changed_at=0)
    db.session.add(admin)
    db.session.commit()
    token = jwt_lib.encode(
        {
            "user": "admin",
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": 0,
            "exp": time.time() + 3600,
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    c = app_with_fed_endpoints.test_client()
    resp = c.get("/test-admin-or-fed", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
