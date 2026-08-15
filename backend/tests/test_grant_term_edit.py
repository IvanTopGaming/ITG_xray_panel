"""The grant decides the key, so editing its term rewrites the key -- manual edits included.

Between actions on the grant the panel does not touch a key at all: an admin can extend one by hand
in the dashboard and it survives, which is how prod ended up with a paused grant beside a key that
had been extended manually for months. That is deliberate -- fixing the key to the grant on a timer
would take hand-editing away entirely. The grant wins only when somebody acts on the grant, and this
endpoint is that action.
"""

from __future__ import annotations

import datetime
import importlib
from unittest.mock import patch

import jwt as jwt_lib
import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Admin, Tariff, TariffItem, TelegramUser, UserTariffAccess
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

        granted = Tariff(name="Premium", price_rub=0, period_days=30, enabled=True)
        private = Tariff(name="Private", price_rub=300, period_days=30, enabled=True, visibility="private")
        db.session.add_all([granted, private])
        db.session.flush()
        db.session.add(TariffItem(tariff_id=granted.id, inbound_tag="hiks", traffic_gb=0, panel_id=2))
        db.session.add(TariffItem(tariff_id=private.id, inbound_tag="hiks", traffic_gb=0, panel_id=2))
        db.session.add_all([TelegramUser(telegram_id=7, language="ru"), TelegramUser(telegram_id=8, language="ru")])
        db.session.add(UserTariffAccess(telegram_id=7, tariff_id=granted.id, billing="free", access_until=None))
        db.session.add(UserTariffAccess(telegram_id=8, tariff_id=private.id, billing="paid"))
        db.session.commit()
        app.config["GRANTED_TARIFF_ID"] = granted.id
        app.config["PRIVATE_TARIFF_ID"] = private.id
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


def _patch_term(master, headers, tg_id, tariff_id, body):
    return master.patch(f"/api/bot/users/{tg_id}/grants/{tariff_id}", headers=headers, json=body)


def test_editing_the_term_rewrites_the_key(master, master_headers, master_app):
    until = "2027-01-31T00:00:00"
    with patch("panel_core.api.bot_admin.apply_tariff_for_user") as applied:
        applied.return_value = {"expires_at_ms": 1, "clients": [], "source": "admin_grant_edit"}
        resp = _patch_term(master, master_headers, 7, master_app.config["GRANTED_TARIFF_ID"], {"access_until": until})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    expected_ms = int(datetime.datetime.fromisoformat(until).timestamp() * 1000)
    assert applied.call_args.kwargs["expiry_ms"] == expected_ms, (
        "the grant is the source of truth for the key -- an edit that only moved the row would leave "
        f"the holder on the old date with the panel claiming otherwise; got {applied.call_args.kwargs}"
    )
    assert resp.get_json()["access_until"].startswith("2027-01-31")


def test_clearing_the_term_makes_the_access_open_ended(master, master_headers, master_app):
    with master_app.app_context():
        grant = UserTariffAccess.query.filter_by(telegram_id=7).first()
        grant.access_until = datetime.datetime(2026, 9, 1)
        db.session.commit()

    with patch("panel_core.api.bot_admin.apply_tariff_for_user") as applied:
        applied.return_value = {"expires_at_ms": 0, "clients": [], "source": "admin_grant_edit"}
        resp = _patch_term(master, master_headers, 7, master_app.config["GRANTED_TARIFF_ID"], {"access_until": None})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert applied.call_args.kwargs["expiry_ms"] == 0, (
        f"clearing the date must put the key back to 'never'; got {applied.call_args.kwargs}"
    )
    assert resp.get_json()["access_until"] is None


def test_editing_a_paid_grant_is_refused(master, master_headers, master_app):
    resp = _patch_term(
        master, master_headers, 8, master_app.config["PRIVATE_TARIFF_ID"], {"access_until": "2027-01-31T00:00:00"}
    )
    assert resp.status_code == 400, (
        "a 'paid' grant provisions nothing -- it only opens a private tariff to purchase, so it has "
        f"no term to edit; got {resp.status_code} {resp.get_data(as_text=True)}"
    )


def test_editing_a_grant_that_does_not_exist_is_404(master, master_headers, master_app):
    resp = _patch_term(master, master_headers, 999, master_app.config["GRANTED_TARIFF_ID"], {"access_until": None})
    assert resp.status_code == 404, resp.get_data(as_text=True)


def test_a_malformed_date_is_refused_without_touching_the_key(master, master_headers, master_app):
    with patch("panel_core.api.bot_admin.apply_tariff_for_user") as applied:
        resp = _patch_term(
            master, master_headers, 7, master_app.config["GRANTED_TARIFF_ID"], {"access_until": "whenever"}
        )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert not applied.called, (
        "a date the panel cannot parse must be refused before the key is rewritten -- provisioning "
        "first would move the holder to a term nobody recorded"
    )


def test_a_node_that_refuses_leaves_the_stored_term_untouched(master, master_headers, master_app):
    from panel_core.services.panel_proxy import RemotePanelError

    def refuse(*_args, **_kwargs):
        raise RemotePanelError(502, "Panel 'Hiks': Panel answered HTTP 502")

    with patch("panel_core.api.bot_admin.apply_tariff_for_user", side_effect=refuse):
        resp = _patch_term(
            master, master_headers, 7, master_app.config["GRANTED_TARIFF_ID"], {"access_until": "2027-01-31T00:00:00"}
        )

    assert resp.status_code == 502, resp.get_data(as_text=True)
    with master_app.app_context():
        grant = UserTariffAccess.query.filter_by(telegram_id=7).first()
        assert grant.access_until is None, (
            "the panel would otherwise show a term the node never received, which is the state this "
            f"whole endpoint exists to prevent; got {grant.access_until!r}"
        )
