import time
import uuid

import jwt
import pytest

from app.extensions import db
from app.models import Admin, Client, DomainStat, Inbound, TrafficSnapshot
from app.utils import SECRET_KEY


@pytest.fixture
def app(app):

    from app.api import statistics as stats_api

    if not any(bp.name == "statistics" for bp in app.blueprints.values()):
        app.register_blueprint(stats_api.bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_token(app):

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


def _seed_inbound(tag="vless-in", port=443, protocol="vless"):
    ib = Inbound(tag=tag, port=port, protocol=protocol, stream_settings="{}")
    db.session.add(ib)
    db.session.flush()
    return ib


def _seed_client(email, inbound_tag, enable=True, up=1000, down=2000):
    c = Client(
        id=str(uuid.uuid4()),
        email=email,
        inbound_tag=inbound_tag,
        enable=enable,
        up=up,
        down=down,
        source_ips="[]",
    )
    db.session.add(c)
    db.session.flush()
    return c


def _seed_snapshot(entity_type, entity_id, inbound_tag, bucket, up, down):
    snap = TrafficSnapshot(
        entity_type=entity_type,
        entity_id=entity_id,
        inbound_tag=inbound_tag,
        bucket=bucket,
        up=up,
        down=down,
    )
    db.session.add(snap)
    db.session.flush()
    return snap


def _seed_domain_stat(domain, date, hit_count, client_email="", inbound_tag=""):
    ds = DomainStat(
        domain=domain,
        date=date,
        hit_count=hit_count,
        client_email=client_email,
        inbound_tag=inbound_tag,
    )
    db.session.add(ds)
    db.session.flush()
    return ds


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _recent_bucket():

    return (int(time.time()) // 3600) * 3600


class TestAuth:
    def test_overview_requires_auth(self, client):
        assert client.get("/api/stats/overview").status_code == 401

    def test_traffic_requires_auth(self, client):
        assert client.get("/api/stats/traffic").status_code == 401

    def test_users_ranking_requires_auth(self, client):
        assert client.get("/api/stats/users-ranking").status_code == 401

    def test_domains_requires_auth(self, client):
        assert client.get("/api/stats/domains").status_code == 401

    def test_domain_users_requires_auth(self, client):
        assert client.get("/api/stats/domain-users?domain=example.com").status_code == 401


class TestOverview:
    def test_overview_empty_db(self, app, client, admin_token):
        resp = client.get("/api/stats/overview", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["period_up"] == 0
        assert body["period_down"] == 0
        assert body["active_users"] == 0
        assert body["total_users"] == 0
        assert body["active_inbounds"] == 0
        assert body["top_users"] == []
        assert body["top_inbounds"] == []
        assert body["top_domains"] == []

    def test_overview_default_period(self, app, client, admin_token):

        bucket = _recent_bucket()
        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_client("alice@test", "vless-in", enable=True, up=500, down=1500)
            _seed_client("bob@test", "vless-in", enable=False, up=100, down=200)
            _seed_snapshot("user", "alice@test", "vless-in", bucket, 1000, 5000)
            _seed_snapshot("user", "bob@test", "vless-in", bucket, 200, 300)
            _seed_snapshot("inbound", "vless-in", "", bucket, 1200, 5300)
            db.session.commit()

        resp = client.get("/api/stats/overview", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["period_up"] == 1200
        assert body["period_down"] == 5300
        assert body["active_users"] == 1
        assert body["total_users"] == 2
        assert body["active_inbounds"] == 1
        assert body["total_up_alltime"] == 600
        assert body["total_down_alltime"] == 1700
        assert len(body["top_users"]) == 2
        assert body["top_users"][0]["email"] == "alice@test"
        assert len(body["top_inbounds"]) == 1
        assert body["top_inbounds"][0]["tag"] == "vless-in"
        assert body["top_inbounds"][0]["protocol"] == "vless"

    def test_overview_custom_period_24h(self, app, client, admin_token):

        now_ts = int(time.time())
        recent_bucket = (now_ts // 3600) * 3600
        old_bucket = recent_bucket - 90 * 86400

        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_client("alice@test", "vless-in")
            _seed_snapshot("user", "alice@test", "vless-in", recent_bucket, 100, 200)
            _seed_snapshot("user", "alice@test", "vless-in", old_bucket, 9999, 9999)
            db.session.commit()

        resp = client.get("/api/stats/overview?period=24h", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["period_up"] == 100
        assert body["period_down"] == 200

    def test_overview_period_all(self, app, client, admin_token):

        now_ts = int(time.time())
        recent_bucket = (now_ts // 3600) * 3600
        old_bucket = recent_bucket - 400 * 86400

        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_client("alice@test", "vless-in")
            _seed_snapshot("user", "alice@test", "vless-in", recent_bucket, 100, 200)
            _seed_snapshot("user", "alice@test", "vless-in", old_bucket, 300, 400)
            db.session.commit()

        resp = client.get("/api/stats/overview?period=all", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["period_up"] == 400
        assert body["period_down"] == 600

    def test_overview_custom_from_to_range(self, app, client, admin_token):
        now_ts = int(time.time())
        bucket_inside = ((now_ts - 3600) // 3600) * 3600
        bucket_outside = ((now_ts - 86400 * 5) // 3600) * 3600
        from_ts = now_ts - 7200
        to_ts = now_ts

        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_client("alice@test", "vless-in")
            _seed_snapshot("user", "alice@test", "vless-in", bucket_inside, 50, 100)
            _seed_snapshot("user", "alice@test", "vless-in", bucket_outside, 9000, 9000)
            db.session.commit()

        resp = client.get(
            f"/api/stats/overview?from={from_ts}&to={to_ts}",
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["period_up"] == 50
        assert body["period_down"] == 100

    def test_overview_from_to_requires_both(self, app, client, admin_token):
        resp = client.get("/api/stats/overview?from=1000", headers=_auth(admin_token))
        assert resp.status_code == 400

    def test_overview_from_to_rejects_invalid(self, app, client, admin_token):
        resp = client.get("/api/stats/overview?from=abc&to=def", headers=_auth(admin_token))
        assert resp.status_code == 400

    def test_overview_from_to_rejects_reversed(self, app, client, admin_token):
        resp = client.get("/api/stats/overview?from=2000&to=1000", headers=_auth(admin_token))
        assert resp.status_code == 400

    def test_overview_top_domains(self, app, client, admin_token):
        from datetime import date

        today = date.today().isoformat()
        with app.app_context():
            _seed_domain_stat("google.com", today, 50)
            _seed_domain_stat("github.com", today, 30)
            db.session.commit()

        resp = client.get("/api/stats/overview", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body["top_domains"]) == 2
        assert body["top_domains"][0]["domain"] == "google.com"
        assert body["top_domains"][0]["hit_count"] == 50


class TestTraffic:
    def test_traffic_empty(self, app, client, admin_token):
        resp = client.get("/api/stats/traffic", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        assert "granularity" in body
        assert body["points"] == []

    def test_traffic_returns_time_series(self, app, client, admin_token):
        bucket = _recent_bucket()
        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_snapshot("user", "alice@test", "vless-in", bucket, 100, 200)
            _seed_snapshot("user", "bob@test", "vless-in", bucket, 50, 80)
            db.session.commit()

        resp = client.get("/api/stats/traffic", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body["points"]) >= 1
        point = body["points"][0]
        assert "ts" in point
        assert "up" in point
        assert "down" in point

        assert point["up"] == 150
        assert point["down"] == 280

    def test_traffic_filter_by_inbound(self, app, client, admin_token):
        bucket = _recent_bucket()
        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_snapshot("inbound", "vless-in", "", bucket, 500, 1000)
            _seed_snapshot("inbound", "other-in", "", bucket, 99, 99)
            db.session.commit()

        resp = client.get(
            "/api/stats/traffic?entity_type=inbound&entity_id=vless-in",
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body["points"]) == 1
        assert body["points"][0]["up"] == 500

    def test_traffic_filter_by_user(self, app, client, admin_token):
        bucket = _recent_bucket()
        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_snapshot("user", "alice@test", "vless-in", bucket, 100, 200)
            _seed_snapshot("user", "bob@test", "vless-in", bucket, 50, 80)
            db.session.commit()

        resp = client.get(
            "/api/stats/traffic?entity_type=user&entity_id=alice@test",
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body["points"]) == 1
        assert body["points"][0]["up"] == 100
        assert body["points"][0]["down"] == 200

    def test_traffic_filter_by_user_and_inbound_tag(self, app, client, admin_token):
        bucket = _recent_bucket()
        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_inbound("trojan-in", 444, "trojan")
            _seed_snapshot("user", "alice@test", "vless-in", bucket, 100, 200)
            _seed_snapshot("user", "alice@test", "trojan-in", bucket, 10, 20)
            db.session.commit()

        resp = client.get(
            "/api/stats/traffic?entity_type=user&entity_id=alice@test&inbound_tag=vless-in",
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body["points"]) == 1
        assert body["points"][0]["up"] == 100

    def test_traffic_custom_range(self, app, client, admin_token):
        now_ts = int(time.time())
        bucket_in = ((now_ts - 3600) // 3600) * 3600
        bucket_out = ((now_ts - 86400 * 10) // 3600) * 3600

        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_snapshot("user", "alice@test", "vless-in", bucket_in, 100, 200)
            _seed_snapshot("user", "alice@test", "vless-in", bucket_out, 9000, 9000)
            db.session.commit()

        from_ts = now_ts - 7200
        to_ts = now_ts
        resp = client.get(
            f"/api/stats/traffic?from={from_ts}&to={to_ts}",
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        body = resp.get_json()
        total_up = sum(p["up"] for p in body["points"])
        assert total_up == 100

    def test_traffic_granularity_scales_with_period(self, app, client, admin_token):
        resp_1h = client.get("/api/stats/traffic?period=1h", headers=_auth(admin_token))
        resp_30d = client.get("/api/stats/traffic?period=30d", headers=_auth(admin_token))
        assert resp_1h.status_code == 200
        assert resp_30d.status_code == 200
        assert resp_1h.get_json()["granularity"] == 600
        assert resp_30d.get_json()["granularity"] == 86400


class TestUsersRanking:
    def test_users_ranking_empty(self, app, client, admin_token):
        resp = client.get("/api/stats/users-ranking", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["users"] == []

    def test_users_ranking_returns_sorted(self, app, client, admin_token):
        bucket = _recent_bucket()
        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_client("alice@test", "vless-in", enable=True, up=0, down=0)
            _seed_client("bob@test", "vless-in", enable=False, up=0, down=0)
            _seed_snapshot("user", "alice@test", "vless-in", bucket, 100, 200)
            _seed_snapshot("user", "bob@test", "vless-in", bucket, 500, 1000)
            db.session.commit()

        resp = client.get("/api/stats/users-ranking", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        users = body["users"]
        assert len(users) == 2

        assert users[0]["email"] == "bob@test"
        assert users[0]["total"] == 1500
        assert users[0]["enable"] is False
        assert users[1]["email"] == "alice@test"
        assert users[1]["total"] == 300
        assert users[1]["enable"] is True

    def test_users_ranking_enrichment(self, app, client, admin_token):

        bucket = _recent_bucket()
        with app.app_context():
            _seed_inbound("vless-in", 443)
            c = _seed_client("alice@test", "vless-in", enable=True)
            c.last_seen = 1234567890
            c.limit_bytes = 10_000_000_000
            c.source_ips = '["1.2.3.4"]'
            _seed_snapshot("user", "alice@test", "vless-in", bucket, 100, 200)
            db.session.commit()

        resp = client.get("/api/stats/users-ranking", headers=_auth(admin_token))
        assert resp.status_code == 200
        user = resp.get_json()["users"][0]
        assert user["last_seen"] == 1234567890
        assert user["limit_bytes"] == 10_000_000_000
        assert user["source_ips"] == ["1.2.3.4"]

    def test_users_ranking_no_client_row(self, app, client, admin_token):

        bucket = _recent_bucket()
        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_snapshot("user", "ghost@test", "vless-in", bucket, 50, 50)
            db.session.commit()

        resp = client.get("/api/stats/users-ranking", headers=_auth(admin_token))
        assert resp.status_code == 200
        user = resp.get_json()["users"][0]
        assert user["email"] == "ghost@test"
        assert user["enable"] is True
        assert user["source_ips"] == []

    def test_users_ranking_custom_period(self, app, client, admin_token):
        now_ts = int(time.time())
        recent_bucket = (now_ts // 3600) * 3600
        old_bucket = recent_bucket - 90 * 86400

        with app.app_context():
            _seed_inbound("vless-in", 443)
            _seed_client("alice@test", "vless-in")
            _seed_snapshot("user", "alice@test", "vless-in", recent_bucket, 100, 200)
            _seed_snapshot("user", "alice@test", "vless-in", old_bucket, 9999, 9999)
            db.session.commit()

        resp = client.get("/api/stats/users-ranking?period=24h", headers=_auth(admin_token))
        assert resp.status_code == 200
        users = resp.get_json()["users"]
        assert len(users) == 1
        assert users[0]["total"] == 300


class TestDomains:
    def test_domains_empty(self, app, client, admin_token):
        resp = client.get("/api/stats/domains", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["domains"] == []

    def test_domains_returns_sorted(self, app, client, admin_token):
        from datetime import date

        today = date.today().isoformat()
        with app.app_context():
            _seed_domain_stat("google.com", today, 100)
            _seed_domain_stat("github.com", today, 50)
            _seed_domain_stat("example.com", today, 200)
            db.session.commit()

        resp = client.get("/api/stats/domains", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        domains = body["domains"]
        assert len(domains) == 3
        assert domains[0]["domain"] == "example.com"
        assert domains[0]["hit_count"] == 200
        assert "percent" in domains[0]

    def test_domains_limit(self, app, client, admin_token):
        from datetime import date

        today = date.today().isoformat()
        with app.app_context():
            for i in range(10):
                _seed_domain_stat(f"site{i}.com", today, 10 + i)
            db.session.commit()

        resp = client.get("/api/stats/domains?limit=3", headers=_auth(admin_token))
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body["domains"]) == 3

    def test_domains_filter_by_email(self, app, client, admin_token):
        from datetime import date

        today = date.today().isoformat()
        with app.app_context():
            _seed_domain_stat("google.com", today, 100, client_email="alice@test")
            _seed_domain_stat("google.com", today, 50, client_email="bob@test")
            db.session.commit()

        resp = client.get("/api/stats/domains?email=alice@test", headers=_auth(admin_token))
        assert resp.status_code == 200
        domains = resp.get_json()["domains"]
        assert len(domains) == 1
        assert domains[0]["hit_count"] == 100

    def test_domains_filter_by_inbound_tag(self, app, client, admin_token):
        from datetime import date

        today = date.today().isoformat()
        with app.app_context():
            _seed_domain_stat("google.com", today, 100, inbound_tag="vless-in")
            _seed_domain_stat("google.com", today, 50, inbound_tag="trojan-in")
            db.session.commit()

        resp = client.get(
            "/api/stats/domains?inbound_tag=vless-in",
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        domains = resp.get_json()["domains"]
        assert len(domains) == 1
        assert domains[0]["hit_count"] == 100

    def test_domains_percent_adds_up(self, app, client, admin_token):
        from datetime import date

        today = date.today().isoformat()
        with app.app_context():
            _seed_domain_stat("google.com", today, 75)
            _seed_domain_stat("github.com", today, 25)
            db.session.commit()

        resp = client.get("/api/stats/domains", headers=_auth(admin_token))
        domains = resp.get_json()["domains"]
        total_pct = sum(d["percent"] for d in domains)
        assert total_pct == pytest.approx(100.0, abs=0.2)


class TestDomainUsers:
    def test_domain_users_requires_domain(self, app, client, admin_token):
        resp = client.get("/api/stats/domain-users", headers=_auth(admin_token))
        assert resp.status_code == 400

    def test_domain_users_empty(self, app, client, admin_token):
        resp = client.get(
            "/api/stats/domain-users?domain=nonexistent.com",
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["domain"] == "nonexistent.com"
        assert body["users"] == []

    def test_domain_users_returns_breakdown(self, app, client, admin_token):
        from datetime import date

        today = date.today().isoformat()
        with app.app_context():
            _seed_domain_stat("google.com", today, 80, client_email="alice@test", inbound_tag="vless-in")
            _seed_domain_stat("google.com", today, 20, client_email="bob@test", inbound_tag="vless-in")
            db.session.commit()

        resp = client.get(
            "/api/stats/domain-users?domain=google.com",
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["domain"] == "google.com"
        users = body["users"]
        assert len(users) == 2
        assert users[0]["email"] == "alice@test"
        assert users[0]["hit_count"] == 80
        assert "percent" in users[0]
        assert users[1]["email"] == "bob@test"
