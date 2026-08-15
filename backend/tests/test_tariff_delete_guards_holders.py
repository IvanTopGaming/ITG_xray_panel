"""Deleting a tariff out from under its holders would leave working keys nobody can revoke.

A grant is tied to its tariff by a cascading foreign key, so deleting the tariff deletes the grant --
while the key stays on the node. That was survivable only because such a key expired within a tariff
period and took itself out. An open-ended one never does, and once the grant row is gone the panel
has nothing left to revoke it with.

Payment history already blocks deletion for the same class of reason. This is the same guard for the
other way a tariff can still matter to somebody.
"""

from __future__ import annotations

import time

import jwt
import pytest

from panel_core.models import Admin, Tariff, TariffItem, UserTariffAccess
from panel_core.utils import SECRET_KEY


@pytest.fixture
def app_with_bot_api(app):
    from panel_core.api import bot_admin

    if not any(bp.name == "bot_admin" for bp in app.blueprints.values()):
        app.register_blueprint(bot_admin.bp, url_prefix="/api")
    return app


@pytest.fixture
def auth_headers(app_with_bot_api, db):
    pwd_version = int(time.time())
    admin = Admin(username="admin", password="x", password_changed_at=pwd_version)
    db.session.add(admin)
    db.session.commit()
    token = jwt.encode(
        {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": pwd_version,
            "exp": int(time.time()) + 3600,
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def tariff(app_with_bot_api, db):
    t = Tariff(name="Premium", price_rub=0, period_days=30, enabled=True)
    db.session.add(t)
    db.session.flush()
    db.session.add(TariffItem(tariff_id=t.id, inbound_tag="hiks", traffic_gb=0, panel_id=2))
    db.session.commit()
    return t


def test_deleting_a_tariff_with_holders_is_refused(app_with_bot_api, db, auth_headers, tariff):
    db.session.add(UserTariffAccess(telegram_id=7, tariff_id=tariff.id, billing="free"))
    db.session.commit()

    resp = app_with_bot_api.test_client().delete(f"/api/bot/tariffs/{tariff.id}/permanent", headers=auth_headers)

    assert resp.status_code == 409, (
        "an open-ended key outlives its tariff row -- deleting the tariff would strand a working key "
        f"with nothing in the database to revoke it; got {resp.status_code} {resp.get_data(as_text=True)}"
    )
    body = resp.get_json()
    assert body["grant_count"] == 1, f"the admin needs to know how many people this affects; got {body!r}"
    assert UserTariffAccess.query.count() == 1, "a refused delete must leave the grant in place"
    assert Tariff.query.count() == 1, "a refused delete must leave the tariff in place"


def test_the_refusal_offers_the_two_ways_out(app_with_bot_api, db, auth_headers, tariff):
    db.session.add(UserTariffAccess(telegram_id=7, tariff_id=tariff.id, billing="free"))
    db.session.commit()

    resp = app_with_bot_api.test_client().delete(f"/api/bot/tariffs/{tariff.id}/permanent", headers=auth_headers)

    hint = resp.get_json().get("hint", "").lower()
    assert "revoke" in hint and "archive" in hint, (
        "a refusal that does not say what to do instead sends the admin looking for a force flag; "
        f"got {resp.get_json()!r}"
    )


def test_a_paid_grant_blocks_deletion_too(app_with_bot_api, db, auth_headers, tariff):
    db.session.add(UserTariffAccess(telegram_id=7, tariff_id=tariff.id, billing="paid"))
    db.session.commit()

    resp = app_with_bot_api.test_client().delete(f"/api/bot/tariffs/{tariff.id}/permanent", headers=auth_headers)

    assert resp.status_code == 409, (
        "a 'paid' grant issues no key, but it is the only record that this user was given access to a "
        f"private tariff -- deleting it silently takes that away; got {resp.status_code}"
    )


def test_deleting_a_tariff_without_holders_still_works(app_with_bot_api, db, auth_headers, tariff):
    resp = app_with_bot_api.test_client().delete(f"/api/bot/tariffs/{tariff.id}/permanent", headers=auth_headers)

    assert resp.status_code == 200, (
        f"a tariff nobody holds and nobody paid for stays deletable; got {resp.get_data(as_text=True)}"
    )
    assert Tariff.query.count() == 0
