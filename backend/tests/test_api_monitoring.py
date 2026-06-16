import time

import jwt
import pytest

from app.utils import SECRET_KEY


@pytest.fixture
def app(app):
    from app.api import monitoring

    if not any(bp.name == "monitoring" for bp in app.blueprints.values()):
        app.register_blueprint(monitoring.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_token(app):
    from app.extensions import db
    from app.models import Admin

    with app.app_context():
        admin = Admin(id=1, username="admin", password="hashed", password_changed_at=0)
        db.session.add(admin)
        db.session.commit()
    token = jwt.encode(
        {"admin_id": 1, "user": "admin", "role": "admin", "pwdv": 0, "exp": time.time() + 3600},
        SECRET_KEY,
        algorithm="HS256",
    )
    return token


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_series_proxied(client, admin_token):
    from unittest.mock import patch

    with patch("app.api.monitoring.default_client") as dc:
        dc.return_value.get.return_value = {"points": [{"ts": 1, "avg": 5}]}
        r = client.get(
            "/api/monitoring/series?metric=cpu_host&scope=host&from=0&to=9",
            headers=_auth(admin_token),
        )
    assert r.status_code == 200
    assert r.get_json()["points"][0]["avg"] == 5


def test_snapshot_enriches_container_names(client, admin_token):
    from unittest.mock import patch

    agent = {
        "series": [
            {"metric": "ram_ctr", "scope": "container", "entity": "abc123", "value": 100},
            {"metric": "cpu_host", "scope": "host", "entity": "", "value": 5},
        ],
        "procs": [],
    }
    with (
        patch("app.api.monitoring.default_client") as dc,
        patch("app.api.monitoring._container_names", return_value={"abc123def": "xray-core"}),
    ):
        dc.return_value.get.return_value = agent
        r = client.get("/api/monitoring/snapshot", headers=_auth(admin_token))
    body = r.get_json()
    ctr = [s for s in body["series"] if s["scope"] == "container"][0]
    assert ctr["name"] == "xray-core"


def test_requires_auth(client):
    assert client.get("/api/monitoring/series").status_code == 401


def test_agent_down_returns_502(client, admin_token):
    import requests
    from unittest.mock import patch

    with patch("app.api.monitoring.default_client") as dc:
        dc.return_value.get.side_effect = requests.RequestException("boom")
        r = client.get("/api/monitoring/snapshot", headers=_auth(admin_token))
    assert r.status_code == 502
