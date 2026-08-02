"""§62: "when does my access end" must have one answer, whichever screen asks it.

A user holds one key per node and each node computes its own expiry (wave 3a), so every surface that
shows a single date folds several into one. Until this wave the two halves of the product folded them
in opposite directions:

    bot          collapse_expiries()   0 absorbs, else **max**   provisioning.py
    sub page     min over `> 0`                                  subscription.py
    client app   min over `> 0`  (the `subscription-userinfo` header)

The same account therefore read one date in Telegram and another in Hiddify, and the gap was widest
in exactly the case §41 created: an unlimited key plus a dated one made the bot say "permanent" while
the app printed a date three days out.

What this file has to prove is **not** "one function is called from three places" -- that assertion
passes on divergent behaviour, because the call sites can feed it different inputs. It compares the
three numbers that actually leave the process, built from **one set of client rows**, on the two role
apps that serve them: `panel-botapi` answers the bot, `panel-sub` answers the page and the header.

The two cases are chosen so that a revert is caught:

- `test_all_three_agree_on_the_nearest_of_two_dates` distinguishes min from max. Homogeneous dates do
  not: with every key on the same day both folds return the same number and the test is green either
  way.
- `test_all_three_call_an_unlimited_account_unlimited` is the mixed case §41 governs. It fails if the
  zero is filtered out anywhere.
"""

from __future__ import annotations

import importlib
import json
import time
import uuid
from unittest.mock import patch

import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Client, Inbound, LinkedPanel, SystemSetting, TelegramUser

from tests.schema import ensure_schema


BOT_TOKEN = "wave5b-bot-service-token"
SUB_TOKEN = "tok42-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TG_ID = 42
DAY_MS = 24 * 3600 * 1000

REALITY_STREAM = json.dumps(
    {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
            "serverNames": ["google.com"],
            "publicKey": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "shortIds": ["abcd1234"],
            "fingerprint": "chrome",
            "spiderX": "",
        },
    }
)


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    try:
        scheduler.remove_all_jobs()
    except Exception:
        pass


@pytest.fixture
def database_url(tmp_path):
    """One database for both roles — in the split deployment sub and bot-api share the Postgres."""

    return ensure_schema(f"sqlite:///{tmp_path}/shared.db")


def _build(role_module, role_env, monkeypatch, tmp_path, database_url):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", role_env)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.com")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)
    return importlib.import_module(role_module).create_app()


@pytest.fixture
def botapi_app(monkeypatch, tmp_path, database_url):
    app = _build("panel_core.roles.botapi", "bot", monkeypatch, tmp_path, database_url)
    with app.app_context():
        db.session.add(SystemSetting(key="bot_service_token", value=BOT_TOKEN))
        db.session.commit()
    return app


@pytest.fixture
def sub_app(monkeypatch, tmp_path, database_url):
    return _build("panel_core.roles.sub", "sub", monkeypatch, tmp_path, database_url)


def _seed(app, expiries):
    """One client per entry, each on its own inbound — the shape a multi-node tariff produces."""

    with app.app_context():
        db.session.add(TelegramUser(telegram_id=TG_ID, sub_token=SUB_TOKEN, language="ru"))
        for i, expiry in enumerate(expiries):
            tag = f"node{i}-reality"
            db.session.add(
                Inbound(tag=tag, label=tag.upper(), protocol="vless", port=443 + i, stream_settings=REALITY_STREAM)
            )
            db.session.add(
                Client(
                    id=str(uuid.uuid4()),
                    email=f"tg{TG_ID}_{tag}",
                    inbound_tag=tag,
                    telegram_id=TG_ID,
                    enable=True,
                    expiry_time=expiry,
                )
            )
        db.session.commit()


def _bot_answer(botapi_app):
    resp = botapi_app.test_client().get(
        f"/api/bot-service/users/{TG_ID}/state",
        headers={"Authorization": f"Bearer {BOT_TOKEN}"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["expires_at_ms"]


def _page_answer(sub_app):
    resp = sub_app.test_client().get(f"/api/sub/u/{SUB_TOKEN}/info")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["expiry_at"]


def _app_header_answer(sub_app):
    """What a client app reads: `expire=` in `subscription-userinfo`, in SECONDS."""

    resp = sub_app.test_client().get(f"/api/sub/u/{SUB_TOKEN}", headers={"User-Agent": "v2rayng"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    field = [p.strip() for p in resp.headers["subscription-userinfo"].split(";") if p.strip().startswith("expire=")]
    assert field, resp.headers["subscription-userinfo"]
    return int(field[0].split("=", 1)[1])


def test_all_three_agree_on_the_nearest_of_two_dates(botapi_app, sub_app):
    now = int(time.time() * 1000)
    soon = now + 3 * DAY_MS
    later = now + 30 * DAY_MS
    _seed(sub_app, [soon, later])

    assert _bot_answer(botapi_app) == soon, (
        "the bot was told the LATEST date while the client app was told the earliest. A user with a "
        "3-day key on one node and a 30-day key on another read '30 days' in Telegram and '3 days' in "
        "Hiddify. Customer decision (wave 5b): the nearest date wins — it answers 'when do I lose the "
        "first thing', and the client app, the one surface with no per-node breakdown, is exactly where "
        "an overstated date leaves a user staring at a dead server."
    )
    assert _page_answer(sub_app) == soon
    assert _app_header_answer(sub_app) == soon // 1000


def test_all_three_call_an_unlimited_account_unlimited(botapi_app, sub_app):
    now = int(time.time() * 1000)
    soon = now + 3 * DAY_MS
    _seed(sub_app, [0, soon])

    assert _bot_answer(botapi_app) == 0, "0 means 'never expires' and absorbs every date (§41)"
    assert _page_answer(sub_app) == 0, (
        "the page filtered the zeroes out before taking the minimum, so an account holding an unlimited "
        "key was shown the date of its shortest dated key while the bot said 'permanent'. That is the "
        "widest form of the §62 gap, and the one a homogeneous fixture never reaches."
    )
    assert _app_header_answer(sub_app) == 0


def test_the_three_still_agree_when_the_key_lives_on_a_node(botapi_app, sub_app, monkeypatch):
    """The real topology: sub and bot-api hold no `Client` row for a node-issued key — they read it
    out of the cached node snapshot, through three separate readers. The fold has to survive that."""

    now = int(time.time() * 1000)
    soon = now + 2 * DAY_MS
    later = now + 40 * DAY_MS
    _seed(sub_app, [later])

    with sub_app.app_context():
        db.session.add(
            LinkedPanel(
                id=3,
                name="Amsterdam",
                url="https://node.example.com",
                federation_token="fed",
                enable=True,
                status="online",
                created_at=0,
            )
        )
        db.session.commit()

    snapshot = {
        "inbounds": [
            {
                "tag": "ams-reality",
                "label": "AMS",
                "port": 8443,
                "protocol": "vless",
                "stream_settings": json.loads(REALITY_STREAM),
                "clients": [
                    {
                        "id": "cccccccc-0000-0000-0000-000000000003",
                        "email": f"tg{TG_ID}_ams",
                        "enable": True,
                        "up": 0,
                        "down": 0,
                        "limit_bytes": 0,
                        "expiry_time": soon,
                        "telegram_id": TG_ID,
                    }
                ],
            }
        ]
    }

    with patch("panel_core.services.panel_proxy.get_panel_snapshot", lambda panel_id: snapshot):
        assert _bot_answer(botapi_app) == soon
        assert _page_answer(sub_app) == soon
        assert _app_header_answer(sub_app) == soon // 1000


def test_a_damaged_row_is_ignored_rather_than_read_as_unlimited(sub_app):
    """§41's other half: `0` is 'never', `NULL` is a row a pre-3a node damaged. Folding NULL in as 0
    would turn one broken client into permanent access for the whole account."""

    now = int(time.time() * 1000)
    soon = now + 5 * DAY_MS
    _seed(sub_app, [soon])
    with sub_app.app_context():
        damaged = str(uuid.uuid4())
        db.session.add(
            Client(
                id=damaged,
                email=f"tg{TG_ID}_damaged",
                inbound_tag="node0-reality",
                telegram_id=TG_ID,
                enable=True,
                expiry_time=1,
            )
        )
        db.session.commit()
        db.session.execute(
            db.text("UPDATE client SET expiry_time = NULL WHERE id = :cid"),
            {"cid": damaged},
        )
        db.session.commit()

    assert _page_answer(sub_app) == soon
    assert _app_header_answer(sub_app) == soon // 1000
