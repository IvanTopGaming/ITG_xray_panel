"""§110 and §111: two ways the master answered without saying anything useful.

An admin grant whose tariff names a node that cannot be reached answered a bare HTML
`500 Internal Server Error` — while the master's log already held the sentence the admin needed
(`Panel answered HTTP 502`). Every federated read and write on the same host was surfacing that
correctly at the same moment; only this path lacked the branch. Observed live by stopping a node's
backend and issuing a grant.

And `GET /api/inbounds` filters by `?panel=`, while the whole rest of the federated surface —
`/system/settings`, `/config`, `/stats/*`, `/outbounds`, every deploy note and CLAUDE.md — uses
`?panel_id=`. A caller who followed the documented name got HTTP 200 and the *whole fleet's*
inbounds, clients and UUIDs, with nothing to indicate the filter had been ignored.
"""

from __future__ import annotations

import datetime
import importlib

import jwt as jwt_lib
import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Admin, LinkedPanel, Tariff, TariffItem
from panel_core.services.panel_proxy import RemotePanelError
from panel_core.utils import SECRET_KEY

from tests.schema import ensure_schema


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


@pytest.fixture
def master(monkeypatch, tmp_path):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/master.db"))
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.com")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)
    app = importlib.import_module("panel_core.roles.master").create_app()

    with app.app_context():
        if Admin.query.first() is None:
            db.session.add(Admin(username="admin", password="x", password_changed_at=0))
        db.session.add(
            LinkedPanel(
                id=1,
                name="Amsterdam",
                url="https://node.example.com",
                federation_token="t",
                created_at=int(datetime.datetime.now(datetime.UTC).timestamp() * 1000),
            )
        )
        tariff = Tariff(id=1, name="Monthly", price_rub=100, period_days=30, visibility="public", enabled=True)
        tariff.items.append(TariffItem(inbound_tag="vless-reality", traffic_gb=10, panel_id=1))
        db.session.add(tariff)
        db.session.commit()
    yield app
    _reset_scheduler()


@pytest.fixture
def headers(master):
    with master.app_context():
        admin = Admin.query.first()
        payload = {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": int(admin.password_changed_at or 0),
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
        }
    return {"Authorization": f"Bearer {jwt_lib.encode(payload, SECRET_KEY, algorithm='HS256')}"}


def test_a_grant_to_an_unreachable_node_names_the_node(master, headers, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise RemotePanelError(502, "Panel 'Amsterdam': Panel answered HTTP 502")

    monkeypatch.setattr("panel_core.api.bot_admin.apply_tariff_for_user", refuse)

    response = master.test_client().post(
        "/api/bot/users/555001/grants", json={"tariff_id": 1, "billing": "gift"}, headers=headers
    )

    assert response.status_code == 502, (
        f"the node's refusal reached the generic handler, so the admin got a bare HTML 500 with no "
        f"panel name and no cause (HTTP {response.status_code})"
    )
    assert response.is_json, "the reply is not JSON, so the UI has nothing to render"
    assert "Amsterdam" in response.get_json()["error"], (
        f"the message does not say which node failed: {response.get_json()!r}"
    )


def test_a_grant_that_fails_leaves_no_phantom_access(master, headers, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise RemotePanelError(502, "Panel 'Amsterdam': Panel answered HTTP 502")

    monkeypatch.setattr("panel_core.api.bot_admin.apply_tariff_for_user", refuse)
    master.test_client().post("/api/bot/users/555001/grants", json={"tariff_id": 1, "billing": "gift"}, headers=headers)

    with master.app_context():
        from panel_core.models import UserTariffAccess

        assert UserTariffAccess.query.filter_by(telegram_id=555001).count() == 0, (
            "a grant was recorded for a user who received nothing, so the panel now believes in access "
            "that does not exist"
        )


def test_a_revoked_token_is_reported_as_a_relink_instruction(master, headers, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise RemotePanelError(401, "invalid or missing federation token")

    monkeypatch.setattr("panel_core.api.bot_admin.apply_tariff_for_user", refuse)

    response = master.test_client().post(
        "/api/bot/users/555002/grants", json={"tariff_id": 1, "billing": "gift"}, headers=headers
    )
    assert response.status_code == 401
    assert "relink" in response.get_json()["error"].lower(), (
        f"a revoked federation token should tell the admin what to do about it: {response.get_json()!r}"
    )


def test_a_node_refusal_reaches_the_admin_verbatim(master, headers, monkeypatch):
    """The node's sentence is the fix instruction — swallowing it leaves the admin with nothing.

    §106 is the worked example: a node refuses a REALITY inbound whose SNI its reverse proxy will
    not route, naming both values and what to change. The master answered `Remote panel error` and
    threw the sentence away, so the guard existed and taught the admin nothing.
    """

    def refuse(*_args, **_kwargs):
        raise RemotePanelError(400, "REALITY SNI 'a' does not match this node's PROXY_DOMAIN 'b'.")

    monkeypatch.setattr("panel_core.api.inbound.proxy_update_inbound", refuse, raising=False)
    monkeypatch.setattr("panel_core.services.panel_proxy.proxy_update_inbound", refuse, raising=False)

    response = master.test_client().put("/api/inbounds/vless-reality?panel_id=1", json={}, headers=headers)

    assert response.status_code == 400, f"the node's own status was replaced by a generic 502 ({response.status_code})"
    assert "PROXY_DOMAIN" in response.get_json()["error"], (
        f"the node explained what to change and the master dropped it: {response.get_json()!r}"
    )


def test_the_inbound_filter_accepts_the_name_the_rest_of_the_api_uses(master, headers):
    response = master.test_client().get("/api/inbounds?panel_id=999", headers=headers)

    assert response.status_code == 400, (
        f"?panel_id was ignored on this one endpoint, so a caller following the documented convention "
        f"was handed every panel's inbounds — clients and UUIDs included — with HTTP "
        f"{response.status_code} and no sign the filter had not applied"
    )
    assert "999" in response.get_json()["error"]


def test_the_original_name_still_works(master, headers):
    assert master.test_client().get("/api/inbounds?panel=local", headers=headers).status_code == 200
    assert master.test_client().get("/api/inbounds?panel=1", headers=headers).status_code == 200


def test_an_unknown_panel_is_refused_rather_than_silently_widened(master, headers):
    response = master.test_client().get("/api/inbounds?panel=999", headers=headers)
    assert response.status_code == 400, (
        f"an unknown panel answered {response.status_code} instead of saying the panel does not exist"
    )
