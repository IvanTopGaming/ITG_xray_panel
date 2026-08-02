"""§10.3: the federation token becomes revocable, and revoking it costs no tariffs.

Two things were missing and they are one procedure. On the node, `generate_link_token` answered
409 once the panel was linked, so a fresh link token could not be issued at all and re-linking
meant editing `federation_config` over SSH. On the master there was no way to point an existing
`LinkedPanel` row at a new token — the only re-link path was delete-and-add, and `delete_panel`
runs `purge_tariff_items`, which removes every `TariffItem` of that panel and disables any tariff
left with none. Revoking a leaked credential would have cost the tariff layout of live users.

The decisive assertions are the two the wave exists for:

* issuing a link token on a linked node kills the current federation token — proven by a **request
  with the old token**, not by reading the row back, so an implementation that updated the wrong
  field or added a second `FederationConfig` still fails here;
* re-linking preserves `TariffItem` rows and leaves tariffs enabled — proven by counting them
  after the call, so an implementation built on delete-and-add fails here.

The node side builds the worker role app **without `DATABASE_URL`**, exactly as
`docker-compose.node.yml` leaves it (§48): setting one points the ORM at a different file from the
one `migrate_sqlite_db` writes to, and the assertions would then read an empty database and pass
for the wrong reason.
"""

import base64
import time
from unittest.mock import MagicMock, patch

import jwt as jwt_lib
import pytest

from panel_core.models import (
    Admin,
    LinkedPanel,
    Tariff,
    TariffItem,
)
from panel_core.utils import SECRET_KEY


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
    from panel_core.extensions import db

    with app.app_context():
        admin = Admin.query.filter_by(username="admin").first()
        if admin is None:
            admin = Admin(username="admin", password="x", password_changed_at=0)
            db.session.add(admin)
            db.session.commit()
        pwd_version = int(admin.password_changed_at or 0)
        admin_id = admin.id

    token = jwt_lib.encode(
        {
            "user": "admin",
            "admin_id": admin_id,
            "role": "admin",
            "pwdv": pwd_version,
            "exp": int(time.time()) + 3600,
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

    return importlib.import_module("panel_core.roles.worker").create_app()


@pytest.fixture
def node(node_app):
    return node_app.test_client()


@pytest.fixture
def node_headers(node_app):
    return _admin_headers(node_app)


def _link_the_node(node, node_headers, master_name="Master"):
    """Run the real procedure: issue a link token, then hand it to the handshake."""

    issued = node.post("/api/federation/link-token", headers=node_headers)
    assert issued.status_code == 200
    composite = issued.get_json()["link_token"]

    raw = base64.urlsafe_b64decode(composite + "=" * (-len(composite) % 4)).decode().split("|", 1)[1]
    shaken = node.post(
        "/api/federation/handshake",
        json={"link_token": raw, "master_url": "https://master.example.com/", "master_name": master_name},
    )
    assert shaken.status_code == 200
    return composite, shaken.get_json()["federation_token"]


class TestTheNodeCanRevoke:
    def test_a_linked_node_still_issues_a_link_token(self, node, node_headers):
        _link_the_node(node, node_headers)

        resp = node.post("/api/federation/link-token", headers=node_headers)

        assert resp.status_code == 200
        assert resp.get_json()["revoked"] is True

    def test_the_old_token_is_dead_the_moment_a_new_link_token_is_issued(self, node, node_headers):
        _, old_token = _link_the_node(node, node_headers)
        assert node.get("/api/federation/snapshot", headers={"X-Federation-Token": old_token}).status_code == 200

        node.post("/api/federation/link-token", headers=node_headers)

        assert node.get("/api/federation/snapshot", headers={"X-Federation-Token": old_token}).status_code == 401

    def test_the_old_token_is_dead_on_provisioning_too_not_only_on_reads(self, node, node_headers):
        _, old_token = _link_the_node(node, node_headers)

        node.post("/api/federation/link-token", headers=node_headers)

        resp = node.post(
            "/api/federation/provision",
            headers={"X-Federation-Token": old_token},
            json={"telegram_id": 1, "inbound_tag": "vless", "period_ms": 1000, "idempotency_key": "k"},
        )
        assert resp.status_code == 401

    def test_the_new_token_works_and_the_old_one_stays_dead(self, node, node_headers):
        _, old_token = _link_the_node(node, node_headers)
        _, new_token = _link_the_node(node, node_headers)

        assert old_token != new_token
        assert node.get("/api/federation/snapshot", headers={"X-Federation-Token": new_token}).status_code == 200
        assert node.get("/api/federation/snapshot", headers={"X-Federation-Token": old_token}).status_code == 401

    def test_issuing_clears_the_linked_state_so_the_card_stops_claiming_a_master(self, node, node_headers, node_app):
        _link_the_node(node, node_headers)

        node.post("/api/federation/link-token", headers=node_headers)

        cfg = node.get("/api/federation/config", headers=node_headers).get_json()
        assert cfg["is_linked"] is False
        assert cfg["linked_at"] is None
        assert cfg["master_url"] == "https://master.example.com/"

    def test_a_link_token_is_still_single_use(self, node, node_headers):
        composite, _ = _link_the_node(node, node_headers)
        raw = base64.urlsafe_b64decode(composite + "=" * (-len(composite) % 4)).decode().split("|", 1)[1]

        again = node.post("/api/federation/handshake", json={"link_token": raw})

        assert again.status_code == 401
        assert "already used" in again.get_json()["error"]

    def test_the_pending_token_is_handed_back_so_the_admin_can_copy_it_again(self, node, node_headers):
        issued = node.post("/api/federation/link-token", headers=node_headers).get_json()["link_token"]

        cfg = node.get("/api/federation/config", headers=node_headers).get_json()

        assert cfg["link_token"] == issued
        assert cfg["is_linked"] is False

    def test_issuing_still_needs_an_admin_login(self, node):
        assert node.post("/api/federation/link-token").status_code == 401

    def test_handshake_is_rate_limited(self, node, node_app):
        from panel_core.extensions import limiter

        with node_app.app_context():
            limiter.reset()
        try:
            codes = {node.post("/api/federation/handshake", json={"link_token": "nope"}).status_code for _ in range(31)}
        finally:
            with node_app.app_context():
                limiter.reset()

        assert 429 in codes


@pytest.fixture
def master_app(app):
    from panel_core.api import panels

    if not any(name == "panels" for name in app.blueprints):
        app.register_blueprint(panels.bp, url_prefix="/api")
    return app


@pytest.fixture
def master(master_app):
    return master_app.test_client()


@pytest.fixture
def master_headers(master_app):
    return _admin_headers(master_app)


def _panel_with_a_tariff(db, token="old-fed-token"):
    panel = LinkedPanel(
        name="child-1",
        url="https://child.example.com",
        federation_token=token,
        created_at=int(time.time() * 1000),
    )
    db.session.add(panel)
    db.session.commit()

    tariff = Tariff(name="Basic", price_rub=100, period_days=30, enabled=True)
    db.session.add(tariff)
    db.session.commit()
    db.session.add(TariffItem(tariff_id=tariff.id, panel_id=panel.id, inbound_tag="vless-reality", traffic_gb=100))
    db.session.commit()
    return panel, tariff


def _handshake_answers(token="fresh-fed-token"):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"federation_token": token}
    return resp


class TestTheMasterRelinksInPlace:
    def test_relink_swaps_the_token_without_touching_the_row(self, master, master_headers, db):
        panel, _ = _panel_with_a_tariff(db)
        panel_id, created_at = panel.id, panel.created_at

        with patch("panel_core.api.panels.requests.post", return_value=_handshake_answers()):
            resp = master.post(
                f"/api/panels/{panel_id}/relink",
                headers=master_headers,
                json={"link_token": "whatever"},
            )

        assert resp.status_code == 200
        refreshed = db.session.get(LinkedPanel, panel_id)
        assert refreshed is not None
        assert refreshed.federation_token == "fresh-fed-token"
        assert LinkedPanel.query.count() == 1
        assert refreshed.created_at == created_at

    def test_relink_keeps_the_tariff_layout_that_deleting_the_panel_would_destroy(self, master, master_headers, db):
        panel, tariff = _panel_with_a_tariff(db)
        panel_id, tariff_id = panel.id, tariff.id

        with patch("panel_core.api.panels.requests.post", return_value=_handshake_answers()):
            resp = master.post(
                f"/api/panels/{panel_id}/relink",
                headers=master_headers,
                json={"link_token": "whatever"},
            )

        assert resp.status_code == 200
        assert TariffItem.query.filter_by(panel_id=panel_id).count() == 1
        assert db.session.get(Tariff, tariff_id).enabled is True

    def test_relink_follows_the_node_to_a_new_address_carried_by_the_token(self, master, master_headers, db):
        panel, _ = _panel_with_a_tariff(db)
        panel_id = panel.id
        composite = base64.urlsafe_b64encode(b"https://moved.example.com|raw-token").decode().rstrip("=")

        with patch("panel_core.api.panels.requests.post", return_value=_handshake_answers()) as mock_post:
            resp = master.post(
                f"/api/panels/{panel_id}/relink",
                headers=master_headers,
                json={"link_token": composite},
            )

        assert resp.status_code == 200
        assert mock_post.call_args[0][0] == "https://moved.example.com/api/federation/handshake"
        assert db.session.get(LinkedPanel, panel_id).url == "https://moved.example.com"

    def test_relink_keeps_the_stored_address_when_the_token_carries_none(self, master, master_headers, db):
        panel, _ = _panel_with_a_tariff(db)
        panel_id = panel.id

        with patch("panel_core.api.panels.requests.post", return_value=_handshake_answers()) as mock_post:
            master.post(
                f"/api/panels/{panel_id}/relink",
                headers=master_headers,
                json={"link_token": "a-bare-token-with-no-url"},
            )

        assert mock_post.call_args[0][0] == "https://child.example.com/api/federation/handshake"
        assert db.session.get(LinkedPanel, panel_id).url == "https://child.example.com"

    def test_a_refused_handshake_leaves_the_old_token_in_place(self, master, master_headers, db):
        panel, _ = _panel_with_a_tariff(db)
        panel_id = panel.id
        refused = MagicMock()
        refused.status_code = 401
        refused.json.return_value = {"error": "no pending link token"}

        with patch("panel_core.api.panels.requests.post", return_value=refused):
            resp = master.post(
                f"/api/panels/{panel_id}/relink",
                headers=master_headers,
                json={"link_token": "whatever"},
            )

        assert resp.status_code == 502
        assert "no pending link token" in resp.get_json()["error"]
        assert db.session.get(LinkedPanel, panel_id).federation_token == "old-fed-token"

    def test_a_handshake_that_returns_no_token_does_not_brick_the_panel(self, master, master_headers, db):
        panel, _ = _panel_with_a_tariff(db)
        panel_id = panel.id

        with patch("panel_core.api.panels.requests.post", return_value=_handshake_answers(token="")):
            resp = master.post(
                f"/api/panels/{panel_id}/relink",
                headers=master_headers,
                json={"link_token": "whatever"},
            )

        assert resp.status_code == 502
        assert db.session.get(LinkedPanel, panel_id).federation_token == "old-fed-token"

    def test_relink_requires_a_token_and_an_existing_panel(self, master, master_headers, db):
        panel, _ = _panel_with_a_tariff(db)

        assert master.post(f"/api/panels/{panel.id}/relink", headers=master_headers, json={}).status_code == 400
        assert (
            master.post("/api/panels/99999/relink", headers=master_headers, json={"link_token": "x"}).status_code == 404
        )

    def test_relink_takes_an_admin_login_and_nothing_else(self, master, db):
        """Not covered by `test_bot_token_scope.py`: that file enumerates one representative
        `panels.py` handler, and its positive branch demands HTTP 200, which a re-link of a panel
        that may not exist cannot give.

        The federation token matters more than the bot one here. `/relink` repoints a panel — and with
        it every `TariffItem` that names it — at whatever address the pasted token carries. Behind
        `admin_or_federation_token_required` a compromised node could aim another panel's tariffs at
        itself, so the decorator choice is load-bearing and pinned here rather than assumed.

        `FederationConfig.federation_token` has to be **set** for that half to mean anything, and this
        was caught by mutation: without it `_check_federation_token` returns False for every token, the
        federation branch falls through to the JWT one, and swapping the decorator leaves the test green.
        A master never fills that column in — wave 0 took the `federation` blueprint off the role, so
        nothing there can — but the row and the column exist on every master, which is the whole point
        of §8.2 and exactly the state this asserts against.
        """

        from panel_core.extensions import db as _db
        from panel_core.models import FederationConfig, SystemSetting

        panel, _ = _panel_with_a_tariff(db)
        cfg = _db.session.get(FederationConfig, 1) or FederationConfig(id=1)
        cfg.federation_token = "master-fed-token-xyz"
        _db.session.add(cfg)
        _db.session.add(SystemSetting(key="bot_service_token", value="bot-token-abc"))
        _db.session.commit()

        path = f"/api/panels/{panel.id}/relink"
        body = {"link_token": "x"}

        assert master.post(path, json=body).status_code == 401
        assert master.post(path, json=body, headers={"Authorization": "Bearer bot-token-abc"}).status_code == 401
        assert master.post(path, json=body, headers={"X-Federation-Token": "master-fed-token-xyz"}).status_code == 401


def test_the_whole_procedure_end_to_end(master, master_headers, node, node_headers, db, monkeypatch):
    """Master → node, both apps live in-process, no mocked handshake.

    This is the only test that proves the composite token an admin copies off the node card is
    the string the master's re-link accepts, address included. Everything else mocks one half.

    `PANEL_DOMAIN` is a public name only from here on: the node app itself has to boot on a local
    domain, because a production-looking one makes `build_base_app` reject the `memory://` limiter
    storage the suite runs with. `_build_panel_url` reads the variable per request, so setting it
    after the app exists is the same thing the node does in production.
    """

    monkeypatch.setenv("PANEL_DOMAIN", "node.example.com")
    monkeypatch.setenv("PANEL_SECRET_PATH", "")

    _, old_token = _link_the_node(node, node_headers)
    panel = LinkedPanel(
        name="child-1",
        url="https://child.example.com",
        federation_token=old_token,
        created_at=int(time.time() * 1000),
    )
    db.session.add(panel)
    db.session.commit()
    panel_id = panel.id

    issued = node.post("/api/federation/link-token", headers=node_headers).get_json()["link_token"]
    assert node.get("/api/federation/snapshot", headers={"X-Federation-Token": old_token}).status_code == 401

    def _through_the_node(url, json=None, **kwargs):
        inner = node.post("/api/federation/handshake", json=json)
        answer = MagicMock()
        answer.status_code = inner.status_code
        answer.json.return_value = inner.get_json()
        return answer

    with patch("panel_core.api.panels.requests.post", side_effect=_through_the_node):
        resp = master.post(f"/api/panels/{panel_id}/relink", headers=master_headers, json={"link_token": issued})

    assert resp.status_code == 200
    relinked = db.session.get(LinkedPanel, panel_id)
    new_token = relinked.federation_token
    assert relinked.url == "https://node.example.com"
    assert new_token != old_token
    assert node.get("/api/federation/snapshot", headers={"X-Federation-Token": new_token}).status_code == 200
    assert node.get("/api/federation/snapshot", headers={"X-Federation-Token": old_token}).status_code == 401
