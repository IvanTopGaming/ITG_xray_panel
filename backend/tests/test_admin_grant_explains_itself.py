"""§76: the one person who can fix an undeliverable tariff is told what is wrong with it.

Wave 5a stopped such a tariff before the money: it is hidden from the catalogue and refused at
checkout. It deliberately left the **admin** paths alone, because widening the check meant another
image. That reason is gone (the branch ships as one major release), and the grant path was the worst
of them: `create_grant` wrapped `apply_tariff_for_user` in nothing, so `LocalXrayUnavailable` -- a
`RuntimeError` -- reached the generic handler and the admin read "Internal server error".

The tariff is not broken in some subtle way. One of its items has no `panel_id`, so it names no node,
and this panel runs no Xray of its own; the start-up audit already prints exactly that to the log.
The admin was the one person who could open Bot -> Tariffs and set it, and the panel told them the
server had fallen over instead.

The master's SPA already surfaces `response.data.error` as a toast on this mutation
(`GrantsTab.tsx`), so no frontend change is involved -- a readable message is enough.
"""

from __future__ import annotations

import datetime
import importlib

import jwt as jwt_lib
import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Admin, Client, Tariff, TariffItem, TelegramUser, UserTariffAccess
from panel_core.utils import SECRET_KEY

from tests.schema import ensure_schema


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    try:
        scheduler.remove_all_jobs()
    except Exception:
        pass


@pytest.fixture
def master_app(monkeypatch, tmp_path):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "master")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/master.db"))
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    app = importlib.import_module("panel_core.roles.master").create_app()
    with app.app_context():
        if Admin.query.first() is None:
            db.session.add(Admin(username="admin", password="x", password_changed_at=0))

        orphan = Tariff(name="Legacy 30d", price_rub=300, period_days=30, enabled=True)
        routed = Tariff(name="Amsterdam 30d", price_rub=300, period_days=30, enabled=True)
        db.session.add_all([orphan, routed])
        db.session.flush()
        db.session.add(TariffItem(tariff_id=orphan.id, inbound_tag="vless-reality", traffic_gb=0, panel_id=None))
        db.session.add(TariffItem(tariff_id=routed.id, inbound_tag="ams-reality", traffic_gb=0, panel_id=9))
        db.session.add(TelegramUser(telegram_id=55, language="ru"))
        db.session.commit()
        app.config["ORPHAN_TARIFF_ID"] = orphan.id
        app.config["ROUTED_TARIFF_ID"] = routed.id
    return app


@pytest.fixture
def master(master_app):
    return master_app.test_client()


@pytest.fixture
def master_headers(master_app):
    with master_app.app_context():
        admin = Admin.query.first()
        payload = {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": int(admin.password_changed_at or 0),
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
        }
    return {"Authorization": f"Bearer {jwt_lib.encode(payload, SECRET_KEY, algorithm='HS256')}"}


def test_granting_an_undeliverable_tariff_says_why(master, master_headers, master_app):
    resp = master.post(
        "/api/bot/users/55/grants",
        headers=master_headers,
        json={"tariff_id": master_app.config["ORPHAN_TARIFF_ID"], "billing": "free"},
    )

    assert resp.status_code == 400, (
        f"granting a tariff whose item names no node answered {resp.status_code}. It used "
        f"to be 500 'Internal server error', which tells the only person who can fix it nothing at "
        f"all.\n\n{resp.get_data(as_text=True)}"
    )
    message = resp.get_json()["error"]
    assert "vless-reality" in message, f"the message must name the offending inbound: {message!r}"
    assert "Legacy 30d" in message, f"the message must name the tariff: {message!r}"
    assert "node" in message.lower()


def test_a_refused_grant_leaves_nothing_behind(master, master_headers, master_app):
    master.post(
        "/api/bot/users/55/grants",
        headers=master_headers,
        json={"tariff_id": master_app.config["ORPHAN_TARIFF_ID"], "billing": "free"},
    )
    with master_app.app_context():
        assert UserTariffAccess.query.count() == 0, (
            "the grant row is added to the session before provisioning runs; without a rollback a "
            "refused grant would still be recorded, and the user would show a tariff nobody issued"
        )
        assert Client.query.count() == 0


def test_a_tariff_routed_to_a_node_still_grants(master, master_headers, master_app, monkeypatch):
    """The negative control. Without it, a handler that refused every grant would look identical."""

    monkeypatch.setattr(
        "panel_core.services.panel_proxy.proxy_provision",
        lambda panel_id, tg, tag, payload: {"expires_at_ms": 1_900_000_000_000, "client": {}},
    )
    resp = master.post(
        "/api/bot/users/55/grants",
        headers=master_headers,
        json={"tariff_id": master_app.config["ROUTED_TARIFF_ID"], "billing": "free"},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    with master_app.app_context():
        assert UserTariffAccess.query.count() == 1
