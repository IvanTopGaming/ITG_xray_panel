"""§8.5 + §7.8 + §7.9 + §20: statistics answered from the machine that counted the traffic.

Only a node collects traffic. `sync_traffic` (10s) and `parse_logs` (15s) run on `roles/worker.py`
and write `traffic_snapshot` / `domain_stat` into that node's **own SQLite**. Nothing has written
either table into the shared Postgres since phase 3b, and the master runs no scheduler at all. So
before this wave the master answered its own five stats endpoints out of two permanently empty
tables — 200 with zeroes on SQLite, and on Postgres a **500** for two of the five, because
`_top_domains_sql` emitted `INDEXED BY`, which is SQLite-only syntax (§20, established by running
the statement against `postgres:16`).

The load-bearing assertion in this file is therefore about **what the master does with no node
named**, and it is the one a naive guard gets wrong: 200-with-zeroes and 200-with-data differ only
in the response body, so `assert status_code == 200` would have passed against the defect. The
master must answer **501** — the same shape `outbound.py` uses for a write it cannot perform
locally — and never a plausible-looking zero.

The second load-bearing assertion is the **order** of dispatch and gate. `has_local_xray()` is
checked *after* the `?panel_id=` dispatch; moving it above is the one mutation that would leave
this whole wave looking finished while every endpoint answered 501 on the master. It is invisible
on a node, where both orders behave identically, so every dispatch assertion here runs against the
**master** role app.

The node app is built **without `DATABASE_URL`** (§48), matching `docker-compose.node.yml`.
"""

import base64
import datetime

import jwt as jwt_lib
import pytest

from panel_core.extensions import db
from panel_core.models import Admin, DomainStat, LinkedPanel, SystemSetting, TrafficSnapshot
from panel_core.utils import SECRET_KEY

from tests.schema import ensure_schema


BOT_TOKEN = "bot-token-must-not-open-the-statistics"

STATS_ENDPOINTS = (
    "/api/stats/overview",
    "/api/stats/traffic",
    "/api/stats/domains",
    "/api/stats/domain-users?domain=example.com",
    "/api/stats/users-ranking",
)


def _current_bucket():
    """Seeded relative to now: the endpoints filter by period, and 1970 is outside every one."""

    return (int(datetime.datetime.now().timestamp()) // 3600) * 3600


def _today():
    return datetime.date.today().isoformat()


def _reset_scheduler():
    from panel_core.extensions import scheduler

    if scheduler.running:
        scheduler.shutdown(wait=False)
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


@pytest.fixture(autouse=True)
def _scheduler_teardown():
    yield
    _reset_scheduler()


def _admin_headers(app):
    with app.app_context():
        admin = Admin.query.first()
        if admin is None:
            admin = Admin(username="admin", password="x", password_changed_at=0)
            db.session.add(admin)
            db.session.commit()
        pwd_version = int(admin.password_changed_at or 0)
        admin_id = admin.id
        username = admin.username

    token = jwt_lib.encode(
        {
            "user": username,
            "admin_id": admin_id,
            "role": "admin",
            "pwdv": pwd_version,
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def node_app(monkeypatch, tmp_path):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "worker")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    import importlib

    app = importlib.import_module("panel_core.roles.worker").create_app()

    with app.app_context():
        db.session.add(SystemSetting(key="bot_service_token", value=BOT_TOKEN))
        db.session.add(
            TrafficSnapshot(
                entity_type="user",
                entity_id="tg42_vless",
                inbound_tag="vless-reality",
                bucket=_current_bucket(),
                up=1_000,
                down=9_000,
            )
        )
        db.session.add(
            DomainStat(
                date=_today(),
                domain="example.com",
                client_email="tg42_vless",
                inbound_tag="vless-reality",
                hit_count=77,
            )
        )
        db.session.commit()

    return app


@pytest.fixture
def node(node_app):
    return node_app.test_client()


@pytest.fixture
def node_headers(node_app):
    return _admin_headers(node_app)


@pytest.fixture
def federation_token(node, node_headers):
    """The real linking procedure, so the token under test is the token the master would send."""

    issued = node.post("/api/federation/link-token", headers=node_headers)
    assert issued.status_code == 200
    composite = issued.get_json()["link_token"]
    raw = base64.urlsafe_b64decode(composite + "=" * (-len(composite) % 4)).decode().split("|", 1)[1]

    shaken = node.post(
        "/api/federation/handshake",
        json={"link_token": raw, "master_url": "https://master.example.com/", "master_name": "Master"},
    )
    assert shaken.status_code == 200
    return shaken.get_json()["federation_token"]


@pytest.fixture
def master_app(monkeypatch, tmp_path):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/master.db"))
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    import importlib

    app = importlib.import_module("panel_core.roles.master").create_app()

    with app.app_context():
        db.session.add(
            LinkedPanel(
                id=7,
                name="Amsterdam",
                url="https://node.example.com",
                federation_token="fed-token",
                status="online",
                enable=True,
                created_at=0,
            )
        )
        db.session.add(
            TrafficSnapshot(
                entity_type="user",
                entity_id="left-over@master",
                inbound_tag="stale",
                bucket=0,
                up=5,
                down=5,
            )
        )
        db.session.commit()

    return app


@pytest.fixture
def master(master_app):
    return master_app.test_client()


@pytest.fixture
def master_headers(master_app):
    return _admin_headers(master_app)


class _RecordingClient:
    """Stands in for the node at the HTTP boundary, so the whole dispatch path still runs."""

    calls = []

    def __init__(self, url, federation_token):
        self.url = url
        self.token = federation_token

    def _record(self, name, params):
        _RecordingClient.calls.append((name, params))

    def stats_overview(self, params):
        self._record("stats_overview", params)
        return {"period_up": 1_000, "period_down": 9_000, "top_domains": [{"domain": "example.com"}]}

    def stats_traffic(self, params):
        self._record("stats_traffic", params)
        return {"granularity": 3600, "points": [{"ts": 0, "up": 1_000, "down": 9_000}]}

    def stats_domains(self, params):
        self._record("stats_domains", params)
        return {"domains": [{"domain": "example.com", "hit_count": 77, "percent": 100.0}]}

    def stats_domain_users(self, params):
        self._record("stats_domain_users", params)
        return {"domain": "example.com", "users": [{"email": "tg42_vless", "hit_count": 77}]}

    def stats_users_ranking(self, params):
        self._record("stats_users_ranking", params)
        return {"users": [{"email": "tg42_vless", "total": 10_000}]}


@pytest.fixture
def node_stub(monkeypatch):
    from panel_core.services import panel_proxy

    _RecordingClient.calls = []
    nudges = []
    monkeypatch.setattr(panel_proxy, "FederationClient", _RecordingClient)
    monkeypatch.setattr(panel_proxy, "_nudge_panel_refresh", lambda pid: nudges.append(pid))
    return {"calls": _RecordingClient.calls, "nudges": nudges}


class TestTheMasterRefusesInsteadOfAnsweringZero:
    """Trap 8: 200-with-zeroes and 200-with-data differ only in the body."""

    @pytest.mark.parametrize("path", STATS_ENDPOINTS)
    def test_no_panel_named_is_a_refusal(self, master, master_headers, path):
        resp = master.get(path, headers=master_headers)

        assert resp.status_code == 501, (
            f"{path} answered HTTP {resp.status_code} on the master. Its `traffic_snapshot` and "
            f"`domain_stat` have had no writer since phase 3b, so any 200 here is a zero shaped "
            f"like an answer."
        )
        assert "no local Xray" in resp.get_json()["error"]
        assert "panel_id" in resp.get_json()["error"], "the refusal must say what to do instead"

    def test_the_refusal_is_not_merely_an_empty_database(self, master_app):
        """A master carrying leftover rows must still refuse — the gate is the role, not the count."""

        with master_app.app_context():
            assert TrafficSnapshot.query.count() == 1

    def test_the_page_is_not_answered_from_the_masters_own_rows(self, master, master_headers):
        resp = master.get("/api/stats/users-ranking", headers=master_headers)

        assert resp.status_code == 501
        assert "left-over@master" not in resp.get_data(as_text=True)


class TestTheMasterAsksTheNode:
    @pytest.mark.parametrize(
        "path,proxy_call",
        [
            ("/api/stats/overview?panel_id=7", "stats_overview"),
            ("/api/stats/traffic?panel_id=7", "stats_traffic"),
            ("/api/stats/domains?panel_id=7", "stats_domains"),
            ("/api/stats/domain-users?panel_id=7&domain=example.com", "stats_domain_users"),
            ("/api/stats/users-ranking?panel_id=7", "stats_users_ranking"),
        ],
    )
    def test_a_named_panel_is_dispatched_and_not_gated(self, master, master_headers, node_stub, path, proxy_call):
        """The gate sits *after* the dispatch. Move it above and every one of these becomes 501."""

        resp = master.get(path, headers=master_headers)

        assert resp.status_code == 200, (
            f"{path} answered HTTP {resp.status_code}; a `has_local_xray()` gate placed before the "
            f"dispatch would produce exactly this and leave the wave looking done"
        )
        assert [name for name, _ in node_stub["calls"]] == [proxy_call]

    def test_the_answer_is_the_nodes_and_not_the_masters(self, master, master_headers, node_stub):
        """Content, not status. The old code also answered 200 — with nothing in it."""

        resp = master.get("/api/stats/overview?panel_id=7", headers=master_headers)

        assert resp.get_json()["period_down"] == 9_000
        assert resp.get_json()["top_domains"] == [{"domain": "example.com"}]

    def test_query_arguments_travel_and_panel_id_does_not(self, master, master_headers, node_stub):
        """`panel_id` is the master's own routing field; forwarding it would be meaningless on a node."""

        master.get(
            "/api/stats/domains?panel_id=7&period=30d&limit=100&inbound_tag=vless-reality",
            headers=master_headers,
        )

        _, params = node_stub["calls"][0]
        assert params == {"period": "30d", "limit": "100", "inbound_tag": "vless-reality"}
        assert "panel_id" not in params

    def test_reading_statistics_does_not_nudge_the_poller(self, master, master_headers, node_stub):
        """Same call as 4c-2's three reads: nothing changed on the node, and this runs on every load."""

        for path in ("/api/stats/overview?panel_id=7", "/api/stats/users-ranking?panel_id=7"):
            master.get(path, headers=master_headers)

        assert node_stub["nudges"] == []

    def test_an_unknown_panel_is_a_400_not_a_crash(self, master, master_headers, node_stub):
        resp = master.get("/api/stats/overview?panel_id=999", headers=master_headers)

        assert resp.status_code == 400
        assert "999" in resp.get_json()["error"]

    def test_a_dead_node_is_reported_with_its_own_status(self, master, master_headers, monkeypatch):
        from panel_core.services import panel_proxy

        def _boom(url, federation_token):
            raise panel_proxy.RemotePanelError(502, "Panel is unreachable: connection refused")

        monkeypatch.setattr(panel_proxy, "FederationClient", _boom)

        resp = master.get("/api/stats/overview?panel_id=7", headers=master_headers)

        assert resp.status_code == 502
        assert "unreachable" in resp.get_json()["error"]


class TestTheNodeAnswersWithItsOwnNumbers:
    @pytest.mark.parametrize("path", STATS_ENDPOINTS)
    def test_the_master_credential_is_accepted(self, node, federation_token, path):
        """All five, not a sample.

        A sample of two left `/stats/users-ranking` free to stay on `token_required`, and the
        mutation run proved it: reverting that one handler kept the whole file green. The bot-token
        parametrisation below cannot stand in for this — it asserts what is refused, and a handler
        that refuses *everyone* satisfies it.
        """

        resp = node.get(path, headers={"X-Federation-Token": federation_token})

        assert resp.status_code == 200, (
            f"{path} rejected the master's federation token (HTTP {resp.status_code}); before this "
            f"wave all five carried `token_required` and the master could not read any of them"
        )

    def test_what_comes_back_is_the_nodes_own_traffic(self, node, federation_token):
        resp = node.get("/api/stats/overview", headers={"X-Federation-Token": federation_token})

        assert resp.get_json()["period_down"] == 9_000

    def test_the_domain_endpoint_works_on_the_node_too(self, node, federation_token):
        resp = node.get("/api/stats/domains?period=365d", headers={"X-Federation-Token": federation_token})

        assert resp.status_code == 200
        assert resp.get_json()["domains"][0]["domain"] == "example.com"

    def test_the_nodes_own_admin_still_gets_in(self, node, node_headers):
        """The node's panel is where an admin without the master's login looks at this page."""

        resp = node.get("/api/stats/users-ranking", headers=node_headers)

        assert resp.status_code == 200
        assert resp.get_json()["users"][0]["email"] == "tg42_vless"

    @pytest.mark.parametrize("path", STATS_ENDPOINTS)
    def test_the_bot_service_token_opens_none_of_them(self, node, path):
        """A real token, seeded into this node's database — a fake one proves nothing (§4a)."""

        resp = node.get(path, headers={"Authorization": f"Bearer {BOT_TOKEN}"})

        assert resp.status_code == 401, (
            f"{path} accepted the bot service token (HTTP {resp.status_code}). It reaches "
            f"/bot-service/* and /billing/checkout, and nothing else."
        )

    @pytest.mark.parametrize("path", STATS_ENDPOINTS)
    def test_an_unauthenticated_request_gets_nothing(self, node, path):
        assert node.get(path).status_code == 401
