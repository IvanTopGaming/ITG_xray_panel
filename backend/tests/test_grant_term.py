"""An admin grants access that outlives the tariff period, and `gift` stops being a third kind.

Three billing kinds differed by which machine kept them alive, not by meaning: `free` was
re-provisioned forever by the cron, `gift` was provisioned once and left to lapse. Both are one grant
with one parameter -- until when -- so the kinds collapse. `paid` is untouched: it provisions nothing
and only opens a private tariff to purchase.

The traffic-reset date is set only for a tariff that actually limits traffic. There is nothing to
zero on an unlimited one, and a date there would have the cron reach that holder's nodes every period
for no effect.
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

        unlimited = Tariff(name="Premium", price_rub=0, period_days=30, enabled=True)
        limited = Tariff(name="Basic", price_rub=125, period_days=30, enabled=True)
        db.session.add_all([unlimited, limited])
        db.session.flush()
        db.session.add(TariffItem(tariff_id=unlimited.id, inbound_tag="hiks", traffic_gb=0, panel_id=2))
        db.session.add(TariffItem(tariff_id=limited.id, inbound_tag="hiks", traffic_gb=300, panel_id=2))
        db.session.add(TelegramUser(telegram_id=55, language="ru"))
        db.session.commit()
        app.config["UNLIMITED_TARIFF_ID"] = unlimited.id
        app.config["LIMITED_TARIFF_ID"] = limited.id
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


def _grant(master, headers, tg_id, payload):
    return master.post(f"/api/bot/users/{tg_id}/grants", headers=headers, json=payload)


def test_open_ended_grant_stores_no_end_date_and_assigns_expiry_zero(master, master_headers, master_app):
    with patch("panel_core.api.bot_admin.apply_tariff_for_user") as applied:
        applied.return_value = {"expires_at_ms": 0, "clients": [], "source": "admin_grant"}
        resp = _grant(
            master,
            master_headers,
            55,
            {"tariff_id": master_app.config["UNLIMITED_TARIFF_ID"], "billing": "free", "access_until": None},
        )

    assert resp.status_code == 201, f"an open-ended grant is the default case; got {resp.get_data(as_text=True)}"
    assert applied.call_args.kwargs["expiry_ms"] == 0, (
        "an open-ended grant must write expiry 0 onto the key -- that is the value the node, the "
        f"expiry evaluator and the limit check all read as 'never'; got {applied.call_args.kwargs}"
    )
    assert resp.get_json()["access_until"] is None, "the reply must report the grant as open-ended"


def test_dated_grant_stores_the_date_and_assigns_it(master, master_headers, master_app):
    until = "2026-12-31T00:00:00"
    with patch("panel_core.api.bot_admin.apply_tariff_for_user") as applied:
        applied.return_value = {"expires_at_ms": 1, "clients": [], "source": "admin_grant"}
        resp = _grant(
            master,
            master_headers,
            55,
            {"tariff_id": master_app.config["UNLIMITED_TARIFF_ID"], "billing": "free", "access_until": until},
        )

    assert resp.status_code == 201, resp.get_data(as_text=True)
    expected_ms = int(datetime.datetime.fromisoformat(until).timestamp() * 1000)
    assert applied.call_args.kwargs["expiry_ms"] == expected_ms, (
        "the admin's date must reach the node verbatim, not as a period added to whatever the holder "
        f"had; got {applied.call_args.kwargs}"
    )
    assert resp.get_json()["access_until"].startswith("2026-12-31")


def test_gift_is_refused(master, master_headers, master_app):
    resp = _grant(
        master, master_headers, 55, {"tariff_id": master_app.config["UNLIMITED_TARIFF_ID"], "billing": "gift"}
    )
    assert resp.status_code == 400, (
        "'gift' folded into 'a grant with a date' -- keeping it would leave two ways to express one "
        f"thing, one of which nothing maintains; got {resp.status_code} {resp.get_data(as_text=True)}"
    )


def test_an_unlimited_tariff_schedules_no_traffic_reset(master, master_headers, master_app):
    with patch("panel_core.api.bot_admin.apply_tariff_for_user") as applied:
        applied.return_value = {"expires_at_ms": 0, "clients": [], "source": "admin_grant"}
        _grant(
            master,
            master_headers,
            55,
            {"tariff_id": master_app.config["UNLIMITED_TARIFF_ID"], "billing": "free", "access_until": None},
        )

    with master_app.app_context():
        grant = UserTariffAccess.query.filter_by(telegram_id=55).first()
        assert grant.next_renewal_at is None, (
            "an unlimited tariff has no counter to zero, so the cron must have no reason to reach "
            f"this holder's nodes at all; got {grant.next_renewal_at!r}"
        )


def test_a_limited_tariff_schedules_its_first_traffic_reset_one_period_out(master, master_headers, master_app):
    with patch("panel_core.api.bot_admin.apply_tariff_for_user") as applied:
        applied.return_value = {"expires_at_ms": 0, "clients": [], "source": "admin_grant"}
        _grant(
            master,
            master_headers,
            55,
            {"tariff_id": master_app.config["LIMITED_TARIFF_ID"], "billing": "free", "access_until": None},
        )

    with master_app.app_context():
        grant = UserTariffAccess.query.filter_by(telegram_id=55).first()
        assert grant.next_renewal_at is not None, "a limited tariff must schedule its first counter reset"
        delta = grant.next_renewal_at - datetime.datetime.utcnow()
        assert 29 <= delta.days <= 30, (
            f"the first reset is one tariff period out; got {grant.next_renewal_at!r} ({delta.days}d)"
        )


def test_a_malformed_date_is_refused_without_provisioning(master, master_headers, master_app):
    with patch("panel_core.api.bot_admin.apply_tariff_for_user") as applied:
        resp = _grant(
            master,
            master_headers,
            55,
            {
                "tariff_id": master_app.config["UNLIMITED_TARIFF_ID"],
                "billing": "free",
                "access_until": "next tuesday",
            },
        )

    assert resp.status_code == 400, f"got {resp.status_code} {resp.get_data(as_text=True)}"
    assert not applied.called, (
        "a date the panel cannot parse must be refused before any key is touched -- provisioning "
        "first and failing afterwards leaves the holder on a term nobody recorded"
    )


def test_a_paid_grant_records_no_term(master, master_headers, master_app):
    private = None
    with master_app.app_context():
        private = Tariff(name="Private", price_rub=300, period_days=30, enabled=True, visibility="private")
        db.session.add(private)
        db.session.flush()
        db.session.add(TariffItem(tariff_id=private.id, inbound_tag="hiks", traffic_gb=0, panel_id=2))
        db.session.commit()
        private_id = private.id

    resp = _grant(master, master_headers, 55, {"tariff_id": private_id, "billing": "paid"})

    assert resp.status_code == 201, resp.get_data(as_text=True)
    assert resp.get_json()["access_until"] is None, (
        "a 'paid' grant provisions nothing -- it only opens a private tariff to purchase, so it has no term of its own"
    )
