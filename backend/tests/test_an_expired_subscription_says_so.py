"""§109: an expired subscription answered `404 User not found` — byte for byte what a dead link answers.

A client app renders that as a failed update, so the one screen a person looks at when the VPN
stops working could not tell them whether their subscription ran out, their link was reset (wave 6
made that a button), or they mistyped the URL. The page at `/info` distinguished all of these and
the app could not, which is the wrong way round: the page has a per-node breakdown, the app has one
line.

The reply is now a config the app accepts, carrying the message where the app will actually show
it -- the name of a server entry -- pointing at a dead address so a tap fails instantly instead of
hanging, with a random UUID so a link that has leaked hands out no credential. `expire` in
`Subscription-Userinfo` is the real, past date, which several clients render as "expired" in the
user's own language without reading our text at all.

**An unknown token still answers 404**, and that half is load-bearing: without it, revoking a leaked
link would look exactly like an expiry, and probing random tokens would get a meaningful answer.
"""

from __future__ import annotations

import base64
import importlib
import urllib.parse

import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Client, Inbound, SystemSetting, TelegramUser

from tests.schema import ensure_schema

TOKEN = "a-live-subscription-token"
APP_UA = {"User-Agent": "v2rayNG/1.8.5"}


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


@pytest.fixture
def sub(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "sub")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/sub.db"))
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.com")
    monkeypatch.setenv("PANEL_DOMAIN", "localhost")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    app = importlib.import_module("panel_core.roles.sub").create_app()
    with app.app_context():
        db.session.add(Inbound(tag="vless-reality", port=443, protocol="vless", stream_settings="{}"))
        db.session.add(TelegramUser(telegram_id=700, sub_token=TOKEN, language="ru"))
        db.session.add(
            Client(
                id="11111111-2222-3333-4444-555555555555",
                email="tg700_vless-reality",
                inbound_tag="vless-reality",
                telegram_id=700,
                enable=False,
                expiry_time=1_600_000_000_000,
                limit_bytes=10,
                up=4,
                down=6,
            )
        )
        db.session.commit()
    yield app
    _reset_scheduler()


def _links(response):
    return base64.b64decode(response.data).decode()


def test_the_app_is_told_the_subscription_ended_instead_of_being_told_nothing(sub):
    response = sub.test_client().get(f"/api/sub/u/{TOKEN}", headers=APP_UA)

    assert response.status_code == 200, (
        f"an expired subscription still answers {response.status_code}; a client app shows that as a "
        f"failed update, which is indistinguishable from a link that no longer exists"
    )
    body = urllib.parse.unquote(_links(response))
    assert "Подписка закончилась" in body, f"the app is given no reason for the outage: {body!r}"


def test_the_message_names_the_bot_when_the_panel_knows_it(sub):
    with sub.app_context():
        db.session.add(SystemSetting(key="bot_username", value="itg_xray_panel_bot"))
        db.session.commit()

    body = urllib.parse.unquote(_links(sub.test_client().get(f"/api/sub/u/{TOKEN}", headers=APP_UA)))
    assert "@itg_xray_panel_bot" in body, (
        f"the user is told to renew but not where; the handle the bot reports on its runtime-config "
        f"poll never reached the message: {body!r}"
    )


def test_the_placeholder_cannot_be_connected_to_and_carries_no_real_key(sub):
    body = urllib.parse.unquote(_links(sub.test_client().get(f"/api/sub/u/{TOKEN}", headers=APP_UA)))

    assert "@127.0.0.1:1" in body, (
        f"the placeholder points somewhere reachable, so tapping it hangs instead of failing: {body!r}"
    )
    assert "11111111-2222-3333-4444-555555555555" not in body, (
        "the user's real UUID was handed out over a link that may be the reason access was revoked"
    )


def test_the_header_carries_the_real_past_expiry(sub):
    response = sub.test_client().get(f"/api/sub/u/{TOKEN}", headers=APP_UA)
    info = response.headers.get("subscription-userinfo", "")

    assert "expire=1600000000" in info, f"clients that render an expiry natively were given nothing to render: {info!r}"


def test_the_expiry_is_taken_from_the_node_when_the_keys_live_there(sub, monkeypatch):
    """In a split deployment every key is on a node, so a header built from local rows is empty.

    `_remote_clients_for_headers` drops disabled clients, which is right for a live subscription and
    exactly wrong here — by the time this reply is being built, being disabled is the whole point.
    """

    from types import SimpleNamespace

    from panel_core.api import subscription as sub_api

    node_client = SimpleNamespace(up=1, down=2, limit_bytes=99, expiry_time=1_500_000_000_000, enable=False)
    monkeypatch.setattr(
        sub_api,
        "_remote_clients_for_headers",
        lambda tg, only_enabled=True: [] if only_enabled else [node_client],
    )

    with sub.app_context():
        Client.query.delete()
        db.session.commit()

    info = sub.test_client().get(f"/api/sub/u/{TOKEN}", headers=APP_UA).headers.get("subscription-userinfo", "")
    assert "expire=1500000000" in info, (
        f"a user whose keys are all on nodes gets no expiry to render, which is every user in the "
        f"shipped topology: {info!r}"
    )


def test_an_unknown_token_still_answers_404(sub):
    response = sub.test_client().get("/api/sub/u/not-a-real-token", headers=APP_UA)

    assert response.status_code == 404, (
        "a token that resolves to nobody now gets the same answer as an expired subscription, so "
        "resetting a leaked link looks like an expiry and probing tokens gets a meaningful reply"
    )


def test_a_blocked_user_is_told_the_same_thing(sub):
    with sub.app_context():
        user = TelegramUser.query.filter_by(telegram_id=700).first()
        user.blocked = True
        db.session.commit()

    response = sub.test_client().get(f"/api/sub/u/{TOKEN}", headers=APP_UA)
    assert response.status_code == 200, "a blocked account still answers 404 rather than saying anything"


def test_a_live_subscription_is_untouched(sub):
    with sub.app_context():
        client = Client.query.first()
        client.enable = True
        client.expiry_time = 4_000_000_000_000
        db.session.commit()

    body = urllib.parse.unquote(_links(sub.test_client().get(f"/api/sub/u/{TOKEN}", headers=APP_UA)))
    assert "Подписка закончилась" not in body, f"a working subscription was replaced by the notice: {body!r}"
    assert "11111111-2222-3333-4444-555555555555" in body, f"the real key went missing: {body!r}"


def test_the_response_declares_its_charset_once(sub):
    response = sub.test_client().get(f"/api/sub/u/{TOKEN}", headers=APP_UA)

    assert response.headers["Content-Type"].count("charset") == 1, (
        f"Flask appends its own charset to a mimetype that already carries one, so every subscription "
        f"response goes out with an invalid header: {response.headers['Content-Type']!r}"
    )
