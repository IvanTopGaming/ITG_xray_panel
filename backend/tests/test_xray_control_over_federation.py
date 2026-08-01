"""§66: the last six node handles the master could not reach.

Waves 4c-1, 4c-2 and 4d handed the master a node's backup, its outbounds, balancers, routing
profiles and traffic figures. Wave 5b made three of the handles in this file stop *claiming
success* -- it put a `has_local_xray()` gate where there had been none. This wave adds the half
above that gate: `?panel_id=` dispatch, the shape 4c-2 established.

Six routes, and the reason each was unreachable is the same one: `token_required` (admin JWT only,
so a node would reject the master's credential) with no branch for `panel_id`.

    GET  /api/system/settings   read the node's Xray log level + GeoIP/GeoSite URLs
    PUT  /api/system/settings   write them
    GET  /api/config            the node's generated /etc/xray/config.json
    POST /api/system/update-geo download the geo databases onto the node
    POST /api/restart           restart the node's Xray
    POST /api/user/routing      pin one user to one outbound on the node

`POST /api/restart` is the odd one: its decorator was already
`admin_or_federation_token_required` (the master has been able to call it since wave 0), it simply
had no dispatch branch and no button anywhere.

**What is load-bearing here, in order.**

1. **The order of dispatch and gate.** `has_local_xray()` is consulted *after* `?panel_id=`.
   Hoisting it above is the single mutation that leaves this wave looking finished while every
   route answers 501 on the master, and it is invisible on a node, where both orders behave
   identically. Every dispatch assertion therefore runs against the **master** role app (§8.5).

2. **Every route asserted individually, never by sample** (§80). The property is held by one line
   per handler and handlers get reverted one at a time; in wave 4d exactly one of thirteen
   mutations stayed green because a sampled assertion stood in for a per-handler one.

3. **A refusal must stay a refusal.** The master with no panel named must answer 501, not a
   plausible-looking 200 -- the class §59 and §20 are both examples of.

4. **`_call_reporting`, not `_call`.** A dead node has to arrive as 502 carrying the node's own
   words; `_call` collapses that into a bare `requests.HTTPError` and the admin reads "Remote panel
   error" (§61). Only the unreachable-node test distinguishes them.

5. **`preferred_outbound` travels in the snapshot.** Without it the master writes the route
   correctly and then displays "Default" forever, because the field the Dashboard reads back comes
   from the federation snapshot, which did not carry it. Write half working, read half lying.

The node app is built **without `DATABASE_URL`** (§48), matching `docker-compose.node.yml`.
"""

from __future__ import annotations

import base64
import datetime
import importlib
import json
import logging
import os

import jwt as jwt_lib
import pytest

from panel_core.extensions import db, limiter, scheduler
from panel_core.models import Admin, Client, Inbound, LinkedPanel, SystemSetting
from panel_core.utils import SECRET_KEY

from tests.schema import ensure_schema


BOT_TOKEN = "bot-token-must-not-open-the-node-xray-surface"

NODE_CONFIG_PATH = "/etc/xray/config.json"

ROUTES = (
    ("get", "/api/system/settings", "proxy_get_system_settings", None),
    ("put", "/api/system/settings", "proxy_update_system_settings", {"xrayLogLevel": "debug"}),
    ("get", "/api/config", "proxy_get_xray_config", None),
    ("post", "/api/system/update-geo", "proxy_update_geo", None),
    ("post", "/api/restart", "proxy_restart_xray", None),
    ("post", "/api/user/routing", "proxy_set_user_routing", {"email": "tg42_vless", "outbound_tag": "direct"}),
)

READS = {"proxy_get_system_settings", "proxy_get_xray_config"}


def _ids(routes):
    return [f"{verb.upper()} {path}" for verb, path, _, _ in routes]


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


def _reset_limits():
    """`/system/update-geo` is 10 per hour and this file calls it far more often than that.

    The storage only exists once some app has run `init_app`, so this is called after each
    `create_app()` rather than from an autouse fixture that runs before them.
    """

    try:
        limiter.reset()
    except Exception:
        pass


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
        payload = {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": int(admin.password_changed_at or 0),
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
        }
    return {"Authorization": f"Bearer {jwt_lib.encode(payload, SECRET_KEY, algorithm='HS256')}"}


@pytest.fixture
def node_app(monkeypatch, tmp_path):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "worker")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    app = importlib.import_module("panel_core.roles.worker").create_app()
    _reset_limits()

    with app.app_context():
        db.session.add(SystemSetting(key="bot_service_token", value=BOT_TOKEN))
        db.session.add(Inbound(tag="vless-reality", port=443, protocol="vless", stream_settings="{}"))
        db.session.add(
            Client(
                id="11111111-2222-3333-4444-555555555555",
                email="tg42_vless",
                inbound_tag="vless-reality",
                telegram_id=42,
                preferred_outbound="amsterdam-egress",
            )
        )
        db.session.commit()

    config = tmp_path / "xray-config.json"
    config.write_text(json.dumps({"inbounds": [{"tag": "vless-reality"}], "log": {"loglevel": "info"}}))
    real_exists, real_open = os.path.exists, open
    monkeypatch.setattr(
        "panel_core.api.system.os.path.exists",
        lambda p: True if p == NODE_CONFIG_PATH else real_exists(p),
    )
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(str(config), *a, **k) if p == NODE_CONFIG_PATH else real_open(p, *a, **k),
    )
    for target in (
        "panel_core.api.system.generate_config_file",
        "panel_core.api.system.restart_xray_container",
        "panel_core.api.system.update_geo_db",
        "panel_core.api.auth.generate_config_file",
        "panel_core.api.auth.restart_xray_container",
    ):
        monkeypatch.setattr(target, lambda *a, **k: None)

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

    app = importlib.import_module("panel_core.roles.master").create_app()
    _reset_limits()

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
        db.session.add(SystemSetting(key="xray_log_level", value="left-over-on-the-master"))
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

    calls: list = []

    def __init__(self, url, federation_token):
        self.url = url
        self.token = federation_token

    def _record(self, name, payload=None):
        _RecordingClient.calls.append((name, payload))

    def get_system_settings(self):
        self._record("get_system_settings")
        return {"xrayLogLevel": "warning", "geoipUrl": "https://node/geoip.dat", "geositeUrl": ""}

    def update_system_settings(self, payload):
        self._record("update_system_settings", payload)
        return {"xrayLogLevel": payload.get("xrayLogLevel", ""), "geoipUrl": "", "geositeUrl": ""}

    def get_xray_config(self):
        self._record("get_xray_config")
        return {"inbounds": [{"tag": "only-on-the-node"}]}

    def update_geo(self):
        self._record("update_geo")
        return {"status": "updated"}

    def restart_xray(self):
        self._record("restart_xray")
        return {"status": "restarted"}

    def set_user_routing(self, payload):
        self._record("set_user_routing", payload)
        return {"status": "updated", "preferred": payload.get("outbound_tag")}


@pytest.fixture
def node_stub(monkeypatch):
    from panel_core.services import panel_proxy

    _RecordingClient.calls = []
    nudges: list = []
    monkeypatch.setattr(panel_proxy, "FederationClient", _RecordingClient)
    monkeypatch.setattr(panel_proxy, "_nudge_panel_refresh", lambda pid: nudges.append(pid))
    return {"calls": _RecordingClient.calls, "nudges": nudges}


def _send(client, verb, path, headers, body):
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, verb)(path, **kwargs)


class TestTheMasterStillRefusesWithNoNodeNamed:
    """Wave 5b's half. It has to survive the wave that builds on top of it."""

    @pytest.mark.parametrize("verb,path,proxy_call,body", ROUTES, ids=_ids(ROUTES))
    def test_no_panel_named_is_a_refusal(self, master, master_headers, verb, path, proxy_call, body):
        resp = _send(master, verb, path, master_headers, body)

        assert resp.status_code == 501, (
            f"{verb.upper()} {path} answered HTTP {resp.status_code} on the master. There is no Xray "
            f"here to read, write, restart or route through; anything but a refusal is a claim about "
            f"a machine this one does not run.\n\n{resp.get_data(as_text=True)}"
        )
        assert "no local Xray" in resp.get_json()["error"]
        assert "panel_id" in resp.get_json()["error"], "the refusal must say what to do instead"

    def test_the_refusal_comes_before_the_write(self, master, master_headers, master_app):
        """A 501 that still stored the value would leave the same lie one layer down."""

        assert master.put("/api/system/settings", headers=master_headers, json={"xrayLogLevel": "debug"}).status_code
        with master_app.app_context():
            assert SystemSetting.query.filter_by(key="xray_log_level").first().value == "left-over-on-the-master"

    def test_the_refusal_is_the_role_and_not_an_empty_database(self, master, master_headers):
        """A master carrying a leftover row must still refuse -- the gate is the role, not the count."""

        resp = master.get("/api/system/settings", headers=master_headers)

        assert resp.status_code == 501
        assert "left-over-on-the-master" not in resp.get_data(as_text=True)


class TestTheMasterAsksTheNode:
    @pytest.mark.parametrize("verb,path,proxy_call,body", ROUTES, ids=_ids(ROUTES))
    def test_a_named_panel_is_dispatched_and_not_gated(
        self, master, master_headers, node_stub, verb, path, proxy_call, body
    ):
        """The gate sits *after* the dispatch. Move it above and every one of these becomes 501."""

        resp = _send(master, verb, f"{path}?panel_id=7", master_headers, body)

        assert resp.status_code == 200, (
            f"{verb.upper()} {path} answered HTTP {resp.status_code}; a `has_local_xray()` gate placed "
            f"before the dispatch produces exactly this and leaves the wave looking done.\n\n"
            f"{resp.get_data(as_text=True)}"
        )
        assert [name for name, _ in node_stub["calls"]] == [proxy_call.replace("proxy_", "", 1)]

    def test_the_settings_answer_is_the_nodes_and_not_the_masters(self, master, master_headers, node_stub):
        """Content, not status. The master's own copy of these keys exists and is meaningless."""

        resp = master.get("/api/system/settings?panel_id=7", headers=master_headers)

        assert resp.get_json()["xrayLogLevel"] == "warning"
        assert resp.get_json()["geoipUrl"] == "https://node/geoip.dat"

    def test_the_config_answer_is_the_nodes(self, master, master_headers, node_stub):
        resp = master.get("/api/config?panel_id=7", headers=master_headers)

        assert resp.get_json()["inbounds"] == [{"tag": "only-on-the-node"}]

    def test_the_body_travels_to_the_node(self, master, master_headers, node_stub):
        master.put("/api/system/settings?panel_id=7", headers=master_headers, json={"geoipUrl": "https://x/geoip.dat"})
        master.post(
            "/api/user/routing?panel_id=7",
            headers=master_headers,
            json={"email": "tg42_vless", "inbound_tag": "vless-reality", "outbound_tag": "amsterdam-egress"},
        )

        assert node_stub["calls"][0] == ("update_system_settings", {"geoipUrl": "https://x/geoip.dat"})
        assert node_stub["calls"][1][1]["outbound_tag"] == "amsterdam-egress"

    @pytest.mark.parametrize(
        "verb,path,proxy_call,body",
        [r for r in ROUTES if r[2] not in READS],
        ids=_ids([r for r in ROUTES if r[2] not in READS]),
    )
    def test_a_write_nudges_the_poller(self, master, master_headers, node_stub, verb, path, proxy_call, body):
        _send(master, verb, f"{path}?panel_id=7", master_headers, body)

        assert node_stub["nudges"] == [7]

    @pytest.mark.parametrize(
        "verb,path,proxy_call,body",
        [r for r in ROUTES if r[2] in READS],
        ids=_ids([r for r in ROUTES if r[2] in READS]),
    )
    def test_a_read_does_not_nudge_the_poller(self, master, master_headers, node_stub, verb, path, proxy_call, body):
        """Same rule as 4c-2's three reads and 4d's five: nothing changed on the node."""

        _send(master, verb, f"{path}?panel_id=7", master_headers, body)

        assert node_stub["nudges"] == []

    @pytest.mark.parametrize("verb,path,proxy_call,body", ROUTES, ids=_ids(ROUTES))
    def test_an_unknown_panel_is_a_400_not_a_crash(
        self, master, master_headers, node_stub, verb, path, proxy_call, body
    ):
        resp = _send(master, verb, f"{path}?panel_id=999", master_headers, body)

        assert resp.status_code == 400
        assert "999" in resp.get_json()["error"]

    @pytest.mark.parametrize("verb,path,proxy_call,body", ROUTES, ids=_ids(ROUTES))
    def test_a_dead_node_arrives_as_502_with_its_own_words(
        self, master, master_headers, monkeypatch, verb, path, proxy_call, body
    ):
        """`_call` would collapse this into a bare HTTPError and the admin would read a 500 (§61)."""

        from panel_core.services import panel_proxy

        def _boom(url, federation_token):
            raise panel_proxy.RemotePanelError(502, "Panel is unreachable: connection refused")

        monkeypatch.setattr(panel_proxy, "FederationClient", _boom)

        resp = _send(master, verb, f"{path}?panel_id=7", master_headers, body)

        assert resp.status_code == 502
        assert "connection refused" in resp.get_json()["error"]

    @pytest.mark.parametrize("verb,path,proxy_call,body", ROUTES, ids=_ids(ROUTES))
    def test_a_revoked_token_says_to_relink(self, master, master_headers, monkeypatch, verb, path, proxy_call, body):
        from panel_core.services import panel_proxy

        def _rejected(url, federation_token):
            raise panel_proxy.RemotePanelError(401, "invalid federation token")

        monkeypatch.setattr(panel_proxy, "FederationClient", _rejected)

        resp = _send(master, verb, f"{path}?panel_id=7", master_headers, body)

        assert resp.status_code == 401
        assert "relink" in resp.get_json()["error"].lower()


class TestTheNodesOwnWordsReachTheAdmin:
    """§61, and the one thing a `FederationClient` stub cannot check.

    Every other test in this file replaces `FederationClient` wholesale, so the choice between
    `_call` and `_call_reporting` is never exercised -- swapping one for the other left the whole
    file green on the first run. The difference only exists at the HTTP boundary: `_call` does
    `raise_for_status()` and hands up a bare `requests.HTTPError` whose text is a status code and a
    URL, so a node saying "Log level 'shout' is not valid" reaches the admin as an Internal Server
    Error. These tests therefore stub **requests**, one layer lower, and let the real client run.
    """

    @pytest.fixture
    def node_answers(self, monkeypatch):
        import requests

        state: dict = {"status": 400, "payload": {"error": "Log level 'shout' is not valid"}}

        class _FakeResponse:
            status_code = property(lambda self: state["status"])

            def json(self):
                return state["payload"]

            def raise_for_status(self):
                if state["status"] >= 400:
                    raise requests.HTTPError(f"{state['status']} Client Error: for url: https://node.example.com")

        for verb in ("get", "post", "put", "delete"):
            monkeypatch.setattr(requests.Session, verb, lambda self, *a, **k: _FakeResponse())
        return state

    @pytest.mark.parametrize("verb,path,proxy_call,body", ROUTES, ids=_ids(ROUTES))
    def test_a_rejected_value_arrives_verbatim_with_the_nodes_code(
        self, master, master_headers, node_answers, verb, path, proxy_call, body
    ):
        resp = _send(master, verb, f"{path}?panel_id=7", master_headers, body)

        assert resp.status_code == 400, (
            f"{verb.upper()} {path} turned the node's HTTP 400 into {resp.status_code}. That is what "
            f"`_call` does: it raises a bare HTTPError and the admin reads a generic failure instead "
            f"of the reason.\n\n{resp.get_data(as_text=True)}"
        )
        assert resp.get_json()["error"] == "Log level 'shout' is not valid"

    def test_a_node_rejecting_the_token_still_says_to_relink(self, master, master_headers, node_answers):
        node_answers["status"] = 401
        node_answers["payload"] = {"error": "invalid federation token"}

        resp = master.get("/api/config?panel_id=7", headers=master_headers)

        assert resp.status_code == 401
        assert "relink" in resp.get_json()["error"].lower()


class TestTheNodeAcceptsTheMastersCredential:
    @pytest.mark.parametrize("verb,path,proxy_call,body", ROUTES, ids=_ids(ROUTES))
    def test_the_master_credential_is_accepted(self, node, federation_token, verb, path, proxy_call, body):
        """All six, not a sample (§80). Five of them carried `token_required` before this wave."""

        resp = _send(node, verb, path, {"X-Federation-Token": federation_token}, body)

        assert resp.status_code == 200, (
            f"{verb.upper()} {path} rejected the master's federation token (HTTP {resp.status_code}); "
            f"before this wave the master could not reach it at all.\n\n{resp.get_data(as_text=True)}"
        )

    def test_the_node_writes_the_setting_into_its_own_database(self, node, federation_token, node_app):
        node.put(
            "/api/system/settings", headers={"X-Federation-Token": federation_token}, json={"xrayLogLevel": "none"}
        )

        with node_app.app_context():
            assert SystemSetting.query.filter_by(key="xray_log_level").first().value == "none"

    def test_the_node_writes_the_route_into_its_own_database(self, node, federation_token, node_app):
        resp = node.post(
            "/api/user/routing",
            headers={"X-Federation-Token": federation_token},
            json={"email": "tg42_vless", "outbound_tag": "direct"},
        )

        assert resp.get_json()["preferred"] == "direct"
        with node_app.app_context():
            assert Client.query.filter_by(email="tg42_vless").first().preferred_outbound == "direct"

    def test_the_nodes_own_admin_still_gets_in(self, node, node_headers):
        """The node's panel is where an admin without the master's login does all of this."""

        assert node.get("/api/system/settings", headers=node_headers).status_code == 200
        assert node.get("/api/config", headers=node_headers).get_json()["inbounds"][0]["tag"] == "vless-reality"

    @pytest.mark.parametrize("verb,path,proxy_call,body", ROUTES, ids=_ids(ROUTES))
    def test_the_bot_service_token_opens_none_of_them(self, node, verb, path, proxy_call, body):
        """A real token, seeded into this node's database -- a fake one proves nothing (§4a)."""

        resp = _send(node, verb, path, {"Authorization": f"Bearer {BOT_TOKEN}"}, body)

        assert resp.status_code == 401, (
            f"{verb.upper()} {path} accepted the bot service token (HTTP {resp.status_code}). It "
            f"reaches /bot-service/* and /billing/checkout, and nothing else."
        )

    @pytest.mark.parametrize("verb,path,proxy_call,body", ROUTES, ids=_ids(ROUTES))
    def test_an_unauthenticated_request_gets_nothing(self, node, verb, path, proxy_call, body):
        assert _send(node, verb, path, {}, body).status_code == 401


class TestTheNodeWritesDownWhoReconfiguredIt:
    """Criterion 4 of the wave. Writes are journalled; so is the one read that hands out secrets."""

    JOURNALLED = (
        ("put", "/api/system/settings", {"xrayLogLevel": "debug"}),
        ("post", "/api/system/update-geo", None),
        ("post", "/api/restart", None),
        ("post", "/api/user/routing", {"email": "tg42_vless", "outbound_tag": "direct"}),
        ("get", "/api/config", None),
    )

    @pytest.mark.parametrize("verb,path,body", JOURNALLED, ids=[f"{v.upper()} {p}" for v, p, _ in JOURNALLED])
    def test_a_federated_call_leaves_a_warning(self, node, federation_token, caplog, verb, path, body):
        with caplog.at_level(logging.INFO):
            _send(node, verb, path, {"X-Federation-Token": federation_token}, body)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "federation token" in r.getMessage()]
        assert warnings, (
            f"{verb.upper()} {path} over the federation token left no WARNING on the node. This log "
            f"line is the only durable record that the master reconfigured it."
        )

    @pytest.mark.parametrize("verb,path,body", JOURNALLED, ids=[f"{v.upper()} {p}" for v, p, _ in JOURNALLED])
    def test_the_nodes_own_admin_leaves_an_info_not_a_warning(self, node, node_headers, caplog, verb, path, body):
        with caplog.at_level(logging.INFO):
            _send(node, verb, path, node_headers, body)

        assert [r for r in caplog.records if r.levelno == logging.INFO and "panel admin" in r.getMessage()]
        assert not [r for r in caplog.records if r.levelno == logging.WARNING and "federation token" in r.getMessage()]

    def test_reading_the_settings_is_not_journalled(self, node, federation_token, caplog):
        """The 4d rule stands for ordinary reads; `GET /api/config` is the named exception, because
        it hands out the REALITY private key and every client UUID for one button press."""

        with caplog.at_level(logging.INFO):
            node.get("/api/system/settings", headers={"X-Federation-Token": federation_token})

        assert not [r for r in caplog.records if "federation token" in r.getMessage()]


class TestTheRouteComesBackForDisplay:
    """The read half of `/user/routing`. Without it the write works and the screen shows Default."""

    def test_the_snapshot_carries_preferred_outbound(self, node, federation_token):
        resp = node.get("/api/federation/snapshot", headers={"X-Federation-Token": federation_token})

        assert resp.status_code == 200
        client = resp.get_json()["inbounds"][0]["clients"][0]
        assert client["preferred_outbound"] == "amsterdam-egress", (
            "the Dashboard reads this field back out of the snapshot; hardcoding it empty makes the "
            "master show 'Default (No preference)' immediately after an admin set a route"
        )

    def test_the_master_overlay_keeps_it(self):
        from panel_core.services.remote_clients import _bucket_panel_clients

        class _Panel:
            id = 7
            name = "Amsterdam"

        bucket: dict = {}
        _bucket_panel_clients(
            bucket,
            {
                "inbounds": [
                    {
                        "tag": "vless-reality",
                        "clients": [{"telegram_id": 42, "email": "tg42_vless", "preferred_outbound": "ams"}],
                    }
                ]
            },
            _Panel(),
        )

        assert bucket[42][0]["preferred_outbound"] == "ams"
