"""§8.9 + §7.12: the node's outbounds, routing profiles and balancers, managed from the master.

Before this wave `outbound.py` and `routing.py` carried **zero** mentions of `panel_id` — no
proxying existed at all — and every handler was `@token_required`, so a node would not have
accepted the master's credential even if the master had tried to send it.

§7.12 recorded that surface as "dead, but it fails loudly". Half of that was wrong, and the wrong
half is what this file is mostly about. Only the **writes** answered 501. The four reads —
`GET /outbounds`, `/outbounds/health`, `/balancers`, `/routing-profiles` — had no
`has_local_xray()` gate at all, so on a master they answered **200 with almost nothing**: the two
seeded outbounds `direct`/`block` for the first two, and `[]` for the rest. Worse, `?panel_id=5`
was accepted and silently dropped, so an admin asking a live node for its outbounds was shown that
node's list as empty. That is the same class as wave 3b's `device_count`: a lie shaped like an
answer.

So the load-bearing assertions here are about **content**, not status codes:

* a read with `?panel_id=` must come back carrying what the *node* holds — a guard that only
  checked for HTTP 200 would have passed against the old code, which also answered 200;
* a write with `?panel_id=` must **not** be 501 on the master. The gate `has_local_xray()` is
  placed *after* the dispatch for exactly this reason, and putting it back above the dispatch is
  the single mutation that would make this whole wave look done while working on no endpoint at
  all. That mutation is invisible on a node, where both orders behave identically, which is why
  every dispatch assertion here runs against the **master** role app;
* the node must accept the master's **real** federation token — obtained through the actual
  link-token → handshake procedure, not written into the row by hand — and a rejected credential
  must be a real one too (`bot_service_token` is seeded into the node's database first). A key
  that would have bounced anyway proves nothing about the decorator; that is how the wave-4b
  assertion passed against a mutated one.

The node app is built **without `DATABASE_URL`** (§48), matching `docker-compose.node.yml`: with
one set, the ORM and `migrate_sqlite_db` address two different files and the assertions read an
empty database and pass for the wrong reason.
"""

import base64
import datetime
import json
import logging

import jwt as jwt_lib
import pytest

from panel_core.extensions import db
from panel_core.models import Admin, Balancer, LinkedPanel, Outbound, RoutingProfile, SystemSetting
from panel_core.utils import SECRET_KEY

from tests.schema import ensure_schema


BOT_TOKEN = "bot-token-must-not-open-the-routing"


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


@pytest.fixture(autouse=True)
def _no_xray_side_effects(monkeypatch):
    """The node role drives a real Xray; no test host has one.

    Patched where they are used (§ the pytest-flask note in CLAUDE.md), so the handlers' bare
    `except Exception` cannot turn a missing `/etc/xray` into a 500 and hide what is asserted.
    """

    for module in ("panel_core.api.outbound", "panel_core.api.routing", "panel_core.api.inbound"):
        monkeypatch.setattr(f"{module}.generate_config_file", lambda *a, **kw: None, raising=False)
        monkeypatch.setattr(f"{module}.restart_xray_container", lambda *a, **kw: None, raising=False)


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

    def _record(self, name, *args):
        _RecordingClient.calls.append((name, *args))

    def list_outbounds(self):
        self._record("list_outbounds")
        return [{"tag": "amsterdam-egress", "protocol": "freedom", "enable": True}]

    def list_balancers(self):
        self._record("list_balancers")
        return [{"tag": "amsterdam-lb", "selector": ["amsterdam-egress"], "strategy": "random"}]

    def list_routing_profiles(self):
        self._record("list_routing_profiles")
        return [{"id": 3, "name": "amsterdam-split", "enable": True, "rules": []}]

    def create_outbound(self, payload):
        self._record("create_outbound", payload)
        return {"tag": payload.get("tag")}

    def update_outbound(self, tag, payload):
        self._record("update_outbound", tag, payload)
        return {"status": "updated"}

    def delete_outbound(self, tag):
        self._record("delete_outbound", tag)
        return {"status": "deleted"}

    def create_balancer(self, payload):
        self._record("create_balancer", payload)
        return {"tag": payload.get("tag")}

    def update_balancer(self, tag, payload):
        self._record("update_balancer", tag, payload)
        return {"status": "updated"}

    def delete_balancer(self, tag):
        self._record("delete_balancer", tag)
        return {"status": "deleted"}

    def create_routing_profile(self, payload):
        self._record("create_routing_profile", payload)
        return {"id": 9, "name": payload.get("name")}

    def update_routing_profile(self, profile_id, payload):
        self._record("update_routing_profile", profile_id, payload)
        return {"status": "updated"}

    def delete_routing_profile(self, profile_id):
        self._record("delete_routing_profile", profile_id)
        return {"status": "deleted"}

    def reset_inbound_traffic(self, tag):
        self._record("reset_inbound_traffic", tag)
        return {"status": "reset"}


@pytest.fixture
def node_stub(monkeypatch):
    from panel_core.services import panel_proxy

    _RecordingClient.calls = []
    nudges = []
    monkeypatch.setattr(panel_proxy, "FederationClient", _RecordingClient)
    monkeypatch.setattr(panel_proxy, "_nudge_panel_refresh", lambda pid: nudges.append(pid))
    return {"calls": _RecordingClient.calls, "nudges": nudges}


WRITES_THROUGH_THE_MASTER = [
    ("post", "/api/outbounds?panel_id=7", {"tag": "eg", "protocol": "freedom"}, "create_outbound"),
    ("put", "/api/outbounds/eg?panel_id=7", {"enable": False}, "update_outbound"),
    ("delete", "/api/outbounds/eg?panel_id=7", None, "delete_outbound"),
    ("post", "/api/balancers?panel_id=7", {"tag": "lb", "selector": ["eg"]}, "create_balancer"),
    ("put", "/api/balancers/lb?panel_id=7", {"enable": False}, "update_balancer"),
    ("delete", "/api/balancers/lb?panel_id=7", None, "delete_balancer"),
    ("post", "/api/routing-profiles?panel_id=7", {"name": "p", "rules": []}, "create_routing_profile"),
    ("put", "/api/routing-profiles/3?panel_id=7", {"enable": False}, "update_routing_profile"),
    ("delete", "/api/routing-profiles/3?panel_id=7", None, "delete_routing_profile"),
    ("post", "/api/inbounds/vless-in/reset-traffic?panel_id=7", None, "reset_inbound_traffic"),
]

READS_THROUGH_THE_MASTER = [
    ("/api/outbounds?panel_id=7", "list_outbounds", "amsterdam-egress"),
    ("/api/balancers?panel_id=7", "list_balancers", "amsterdam-lb"),
    ("/api/routing-profiles?panel_id=7", "list_routing_profiles", "amsterdam-split"),
]

ALL_HANDLES = [
    ("get", "/api/outbounds"),
    ("post", "/api/outbounds"),
    ("put", "/api/outbounds/eg"),
    ("delete", "/api/outbounds/eg"),
    ("get", "/api/balancers"),
    ("post", "/api/balancers"),
    ("put", "/api/balancers/lb"),
    ("delete", "/api/balancers/lb"),
    ("get", "/api/routing-profiles"),
    ("post", "/api/routing-profiles"),
    ("put", "/api/routing-profiles/3"),
    ("delete", "/api/routing-profiles/3"),
    ("post", "/api/inbounds/vless-in/reset-traffic"),
]


class TestTheMasterReachesTheNodeInsteadOfAnsweringForIt:
    @pytest.mark.parametrize(
        "url,method_name,expected_tag", READS_THROUGH_THE_MASTER, ids=[r[1] for r in READS_THROUGH_THE_MASTER]
    )
    def test_a_read_scoped_to_a_panel_returns_what_that_panel_holds(
        self, master, master_headers, node_stub, url, method_name, expected_tag
    ):
        """The defect this wave exists for: the old code answered 200 with the master's own rows.

        Asserting the status code alone reproduces exactly that pass — `?panel_id=` used to be
        parsed off and dropped, and the reply was still HTTP 200. Only the body separates the two.
        """

        resp = master.get(url, headers=master_headers)

        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = json.dumps(resp.get_json())
        assert expected_tag in body, f"the master answered for the node instead of asking it: {body}"
        assert method_name in [c[0] for c in node_stub["calls"]]

    @pytest.mark.parametrize(
        "method,url,payload,method_name",
        WRITES_THROUGH_THE_MASTER,
        ids=[w[3] for w in WRITES_THROUGH_THE_MASTER],
    )
    def test_a_write_scoped_to_a_panel_is_dispatched_before_the_local_xray_gate(
        self, master, master_headers, node_stub, method, url, payload, method_name
    ):
        """Trap 1, and the reason every assertion in this class runs on the *master* app.

        A master has no local Xray, so a `has_local_xray()` gate placed above the dispatch answers
        501 on every one of these before `panel_id` is ever looked at — the wave would look
        finished and route nothing. On a node the two orders are indistinguishable.
        """

        kwargs = {"json": payload} if payload is not None else {}
        resp = getattr(master, method)(url, headers=master_headers, **kwargs)

        assert resp.status_code != 501, "the local-Xray gate ran before the panel_id dispatch"
        assert resp.status_code in (200, 201), resp.get_data(as_text=True)
        assert method_name in [c[0] for c in node_stub["calls"]]

    def test_a_write_without_a_panel_id_still_refuses_and_names_the_way_out(self, master, master_headers):
        resp = master.post("/api/outbounds", headers=master_headers, json={"tag": "x", "protocol": "freedom"})

        assert resp.status_code == 501
        assert "panel_id" in resp.get_json()["error"]

    def test_an_unknown_panel_is_a_refusal_not_an_empty_list(self, master, master_headers):
        resp = master.get("/api/outbounds?panel_id=999", headers=master_headers)

        assert resp.status_code == 400
        assert "999" in resp.get_json()["error"]

    def test_a_node_that_does_not_answer_is_reported_not_hidden(self, master, master_headers, monkeypatch):
        """The customer's decision: an explicit error, never an empty list.

        An empty list reads as "this node has no outbounds" and invites an admin to create them a
        second time on top of the ones already there.
        """

        from panel_core.services import panel_proxy

        class _Dead:
            def __init__(self, *a, **kw):
                pass

            def list_outbounds(self):
                raise panel_proxy.RemotePanelError(502, "Panel is unreachable: connection timed out")

        monkeypatch.setattr(panel_proxy, "FederationClient", _Dead)

        resp = master.get("/api/outbounds?panel_id=7", headers=master_headers)

        assert resp.status_code == 502
        assert "unreachable" in resp.get_json()["error"]

    def test_a_node_that_rejects_the_token_says_how_to_fix_it(self, master, master_headers, monkeypatch):
        from panel_core.services import panel_proxy

        class _Rejecting:
            def __init__(self, *a, **kw):
                pass

            def list_outbounds(self):
                raise panel_proxy.RemotePanelError(401, "Invalid federation token")

        monkeypatch.setattr(panel_proxy, "FederationClient", _Rejecting)

        resp = master.get("/api/outbounds?panel_id=7", headers=master_headers)

        assert resp.status_code == 401
        assert "relink" in resp.get_json()["error"].lower()

    def test_the_nodes_own_validation_message_reaches_the_admin(self, master, master_headers, monkeypatch):
        """Without this the master answers "Remote panel error: 400 Client Error" and the admin
        never learns that the tag was simply taken."""

        from panel_core.services import panel_proxy

        class _Refusing:
            def __init__(self, *a, **kw):
                pass

            def create_outbound(self, payload):
                raise panel_proxy.RemotePanelError(400, "Tag exists")

        monkeypatch.setattr(panel_proxy, "FederationClient", _Refusing)

        resp = master.post(
            "/api/outbounds?panel_id=7", headers=master_headers, json={"tag": "eg", "protocol": "freedom"}
        )

        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Tag exists"


class TestOnlyChangesNudgeTheCron:
    """Every mutating proxy publishes on `panel:refresh`; a read must not.

    Reads happen on every page load, and nudging on one would have the cron poll that node out of
    band each time an admin looks at a list. `DEL` on the snapshot key is never an option here —
    for the sub role a missing key means "this panel has no remote clients", not "stale", so a
    subscription would silently shrink (§7.1).
    """

    @pytest.mark.parametrize(
        "method,url,payload,method_name",
        WRITES_THROUGH_THE_MASTER,
        ids=[w[3] for w in WRITES_THROUGH_THE_MASTER],
    )
    def test_a_change_asks_the_cron_to_repoll_that_node(
        self, master, master_headers, node_stub, method, url, payload, method_name
    ):
        kwargs = {"json": payload} if payload is not None else {}
        getattr(master, method)(url, headers=master_headers, **kwargs)

        assert node_stub["nudges"] == [7]

    @pytest.mark.parametrize(
        "url,method_name,_tag", READS_THROUGH_THE_MASTER, ids=[r[1] for r in READS_THROUGH_THE_MASTER]
    )
    def test_a_read_does_not(self, master, master_headers, node_stub, url, method_name, _tag):
        master.get(url, headers=master_headers)

        assert node_stub["nudges"] == []


class TestTheNodeAcceptsTheMastersCredential:
    def test_the_whole_network_of_a_node_can_be_built_over_the_federation_token(self, node, federation_token, node_app):
        """The wave's one-sentence claim, end to end on the node side.

        An outbound, a balancer over it and a routing profile pointing at that balancer — the
        three things §8.9 says an admin must be able to set up without logging into the node.
        """

        headers = {"X-Federation-Token": federation_token}

        created = node.post("/api/outbounds", headers=headers, json={"tag": "eg", "protocol": "freedom"})
        assert created.status_code == 201, created.get_data(as_text=True)

        balanced = node.post(
            "/api/balancers", headers=headers, json={"tag": "lb", "selector": ["eg"], "strategy": "random"}
        )
        assert balanced.status_code == 201, balanced.get_data(as_text=True)

        profiled = node.post(
            "/api/routing-profiles",
            headers=headers,
            json={"name": "split", "rules": [{"domain": ["example.com"], "outboundTag": "lb"}]},
        )
        assert profiled.status_code == 201, profiled.get_data(as_text=True)

        with node_app.app_context():
            assert Outbound.query.filter_by(tag="eg").one().protocol == "freedom"
            assert json.loads(Balancer.query.filter_by(tag="lb").one().selector) == ["eg"]
            stored = RoutingProfile.query.filter_by(name="split").one()
            assert json.loads(stored.rules)[0]["outboundTag"] == "lb"

    def test_the_master_can_read_back_what_it_wrote(self, node, federation_token):
        headers = {"X-Federation-Token": federation_token}
        node.post("/api/outbounds", headers=headers, json={"tag": "eg", "protocol": "freedom"})

        listed = node.get("/api/outbounds", headers=headers)

        assert listed.status_code == 200
        assert "eg" in [o["tag"] for o in listed.get_json()]

    def test_the_master_can_take_it_all_away_again(self, node, federation_token):
        headers = {"X-Federation-Token": federation_token}
        node.post("/api/outbounds", headers=headers, json={"tag": "eg", "protocol": "freedom"})
        node.post("/api/routing-profiles", headers=headers, json={"name": "split", "rules": []})
        profile_id = node.get("/api/routing-profiles", headers=headers).get_json()[0]["id"]

        assert node.delete(f"/api/routing-profiles/{profile_id}", headers=headers).status_code == 200
        assert node.delete("/api/outbounds/eg", headers=headers).status_code == 200
        assert node.get("/api/outbounds", headers=headers).get_json() == [
            {
                "tag": "direct",
                "protocol": "freedom",
                "enable": True,
                "settings": {},
                "streamSettings": {},
                "mux": {},
                "send_through": "",
                "public_ip": "",
                "gateway": "",
            },
            {
                "tag": "block",
                "protocol": "blackhole",
                "enable": True,
                "settings": {},
                "streamSettings": {},
                "mux": {},
                "send_through": "",
                "public_ip": "",
                "gateway": "",
            },
        ]

    def test_a_revoked_token_stops_reaching_the_routing(self, node, node_headers, federation_token):
        headers = {"X-Federation-Token": federation_token}
        assert node.get("/api/routing-profiles", headers=headers).status_code == 200

        node.post("/api/federation/link-token", headers=node_headers)

        assert node.get("/api/routing-profiles", headers=headers).status_code == 401
        assert node.post("/api/outbounds", headers=headers, json={"tag": "x"}).status_code == 401


class TestTheBotTokenOpensNoneOfIt:
    """§51: the bot token used to reach twenty-one admin endpoints through a third branch inside
    `admin_or_federation_token_required`. This wave hangs that decorator on thirteen more, so the
    same question has to be asked again — and asked with a **real** token, seeded into the node's
    own database, or the assertion passes on a mutated decorator too."""

    def test_the_seeded_token_is_the_real_one(self, node_app):
        with node_app.app_context():
            stored = SystemSetting.query.filter_by(key="bot_service_token").first()
            assert stored is not None and stored.value == BOT_TOKEN

    @pytest.mark.parametrize("method,url", ALL_HANDLES, ids=[f"{m}{u}" for m, u in ALL_HANDLES])
    def test_the_bot_token_is_refused_as_a_bearer(self, node, method, url):
        resp = getattr(node, method)(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"}, json={})

        assert resp.status_code == 401, resp.get_data(as_text=True)

    @pytest.mark.parametrize("method,url", ALL_HANDLES, ids=[f"{m}{u}" for m, u in ALL_HANDLES])
    def test_the_bot_token_is_refused_as_a_federation_token(self, node, method, url):
        resp = getattr(node, method)(url, headers={"X-Federation-Token": BOT_TOKEN}, json={})

        assert resp.status_code == 401, resp.get_data(as_text=True)


class TestOutboundHealthStaysWhereTheTrafficIs:
    """Customer decision (4c-1, item 5). A probe is only meaningful from the box the traffic
    leaves through, so the master neither runs it nor proxies it."""

    def test_the_master_refuses_and_says_where_to_look(self, master, master_headers):
        resp = master.get("/api/outbounds/health", headers=master_headers)

        assert resp.status_code == 501
        assert "node" in resp.get_json()["error"]

    def test_it_is_not_quietly_proxied_when_a_panel_is_named(self, master, master_headers, node_stub):
        resp = master.get("/api/outbounds/health?panel_id=7", headers=master_headers)

        assert resp.status_code == 501
        assert node_stub["calls"] == []

    def test_the_node_still_probes_its_own(self, node, node_headers):
        resp = node.get("/api/outbounds/health", headers=node_headers)

        assert resp.status_code == 200
        assert {item["tag"] for item in resp.get_json()} == {"direct", "block"}

    def test_the_federation_token_does_not_open_it(self, node, federation_token):
        resp = node.get("/api/outbounds/health", headers={"X-Federation-Token": federation_token})

        assert resp.status_code == 401


class TestRewritingANodesRoutingIsWrittenDown:
    """§10.3: this wave widens the federation token from "the node's users" to "where the node's
    traffic goes" — a change that leaves no trace in any client list. The node writes down which
    credential did it (customer decision, no new tables: §40 stays untouched)."""

    def test_a_federated_routing_change_leaves_a_warning(self, node, federation_token, caplog):
        with caplog.at_level(logging.WARNING, logger="panel_core.api.routing"):
            node.post("/api/routing-profiles", headers={"X-Federation-Token": federation_token}, json={"name": "p"})

        federated = [r for r in caplog.records if "federation" in r.getMessage()]
        assert federated, "a federated routing change must name the credential it used"
        assert "'p'" in federated[0].getMessage()

    def test_a_federated_outbound_change_leaves_a_warning(self, node, federation_token, caplog):
        with caplog.at_level(logging.WARNING, logger="panel_core.api.outbound"):
            node.post(
                "/api/outbounds",
                headers={"X-Federation-Token": federation_token},
                json={"tag": "eg", "protocol": "freedom"},
            )

        assert [r for r in caplog.records if "federation" in r.getMessage()]

    def test_the_nodes_own_admin_does_not_raise_the_same_alarm(self, node, node_headers, caplog):
        with caplog.at_level(logging.INFO, logger="panel_core.api.outbound"):
            node.post("/api/outbounds", headers=node_headers, json={"tag": "eg", "protocol": "freedom"})

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [r for r in caplog.records if "panel admin" in r.getMessage()]
