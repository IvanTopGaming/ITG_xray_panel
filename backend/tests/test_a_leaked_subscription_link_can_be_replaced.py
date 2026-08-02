"""§10.3: `sub_token` was the one long-lived secret nothing could change.

For a product where the subscription URL *is* the access, that is a gap you notice only once it
matters: a user posts their link in a chat, and the only remedy is an UPDATE against the database by
hand. Every other credential in the deployment has some answer — the admin JWT dies when the password
changes, the federation token is revoked from the node's own System page (wave 4b), the bot service
token has a button.

Three things have to hold together, and each is a different failure if it is the one missing.

1. **The old link must die.** Not "expire soon" — die. Every subscription route resolves the account
   by token against the database before it consults any cache, so replacing the value is enough; but
   the cached responses keyed by the *old* token are dropped as well, and dropped **before** the
   value changes, because afterwards there is nothing left to name them with.
2. **The user must be told.** Their app will simply stop updating, hours or days later, with no
   explanation and nothing they did to cause it. A silent reset trades a leaked link for a support
   ticket, which is the "quiet lie" class waves 5a-5c spent three rounds removing.
3. **Their access must be untouched.** This rotates a URL, not a subscription: the same clients, the
   same keys, the same expiry, reachable through a different address.
"""

from __future__ import annotations

import datetime
import importlib
import pathlib

import jwt as jwt_lib
import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Admin, BotEvent, Client, Inbound, TelegramUser
from panel_core.utils import SECRET_KEY

from tests.schema import ensure_schema


REPO = pathlib.Path(__file__).resolve().parents[2]

OLD_TOKEN = "the-token-that-leaked"


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    try:
        scheduler.remove_all_jobs()
    except Exception:
        pass


@pytest.fixture
def master(monkeypatch, tmp_path):
    """The route ships from `panel-master`, so the assertions run against that role's app."""

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
        db.session.add(Inbound(tag="vless-reality", port=443, protocol="vless", stream_settings="{}"))
        db.session.add(TelegramUser(telegram_id=700, sub_token=OLD_TOKEN, language="en"))
        db.session.add(
            Client(
                id="11111111-2222-3333-4444-555555555555",
                email="tg700_vless",
                inbound_tag="vless-reality",
                telegram_id=700,
                enable=True,
                expiry_time=1900000000000,
            )
        )
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


def _reset(master, headers, tg_id=700):
    return master.test_client().post(f"/api/bot/users/{tg_id}/reset-sub-token", headers=headers)


def test_the_token_changes(master, headers):
    response = _reset(master, headers)
    assert response.status_code == 200
    assert response.get_json()["sub_url"], "the reply carries no new URL for the admin to look at"

    with master.app_context():
        fresh = TelegramUser.query.filter_by(telegram_id=700).first()
    assert fresh.sub_token != OLD_TOKEN, "the leaked token is still the account's subscription link"
    assert fresh.sub_token


def test_the_old_link_stops_resolving(master, headers):
    _reset(master, headers)

    with master.app_context():
        stale = TelegramUser.query.filter_by(sub_token=OLD_TOKEN).first()
    assert stale is None, (
        "the old token still resolves to the account, so whoever has the leaked URL still has the "
        "subscription — which is the whole point of the button."
    )


def test_the_cache_is_dropped_for_the_old_token_not_the_new_one(master, headers, monkeypatch):
    """Invalidation reads the token from the row, so it has to happen before the row changes."""

    from panel_core.services import sub_cache

    seen: list[int] = []
    order: list[str] = []

    def fake_invalidate(telegram_id):
        seen.append(telegram_id)
        row = TelegramUser.query.filter_by(telegram_id=telegram_id).first()
        order.append(row.sub_token)

    monkeypatch.setattr(sub_cache, "invalidate_user_aggregate", fake_invalidate)
    _reset(master, headers)

    assert seen == [700], "the subscription cache was not invalidated at all"
    assert order == [OLD_TOKEN], (
        "invalidation ran after the token had already been replaced, so it cleared keys for the NEW "
        "token and left the old ones to serve the leaked link until they expire."
    )


def test_the_user_is_told(master, headers):
    _reset(master, headers)

    with master.app_context():
        events = BotEvent.query.filter_by(type="sub_link_reset", telegram_id=700).all()
    assert len(events) == 1, (
        "no event was published, so the user's app stops updating with no message and no cause they can see."
    )
    payload = events[0].payload or {}
    assert payload.get("lang") == "en", "the notification would be sent in the wrong language"


def test_the_consumer_has_a_branch_for_it():
    """Wave 5b's rule: nothing is published that the consumer has no branch for (§69)."""

    consumer = (REPO / "tg_bot" / "bot_events_consumer.py").read_text()
    assert 'etype == "sub_link_reset"' in consumer, (
        "the event has no branch in the bot, so it falls into the dispatcher's `else: return` and "
        "only fills a row in bot_event until the cleanup cron prunes it."
    )
    texts = (
        REPO / "backend" / "packages" / "panel-core" / "src" / "panel_core" / "data" / "bot_texts_defaults.yaml"
    ).read_text()
    assert "notification.sub_link_reset:" in texts, "the branch renders a text key that does not exist"


def test_access_is_not_touched(master, headers):
    """It rotates a URL. A user whose keys were revoked by a link reset would be a far worse bug."""

    with master.app_context():
        before = [(c.id, c.enable, c.expiry_time) for c in Client.query.filter_by(telegram_id=700).all()]
    _reset(master, headers)
    with master.app_context():
        after = [(c.id, c.enable, c.expiry_time) for c in Client.query.filter_by(telegram_id=700).all()]
        blocked = TelegramUser.query.filter_by(telegram_id=700).first().blocked

    assert before == after and before, "resetting the link changed the user's clients"
    assert blocked is False


def test_an_unknown_user_is_a_404(master, headers):
    assert _reset(master, headers, tg_id=999999).status_code == 404


def test_it_needs_an_admin(master):
    assert _reset(master, {}).status_code == 401
