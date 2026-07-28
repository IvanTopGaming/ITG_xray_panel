"""Wave 4a: the bot gets its keys back, and it gets them from bot-api.

Every test here builds the **bot-api** role's app. That is load-bearing twice over. The links
have to be built where the bot can reach them -- `/bot-service/*` lives on this role and nowhere
else since phase 3c-2 -- and they have to be built out of the node snapshot, because a client
issued on a node has no `Client` row in this role's Postgres at all (`Client` carries no
`panel_id` and the master mirrors none). A test that used a worker-shaped app would find a local
row, build a link from it, and prove nothing about the path that was actually broken.

The clients here therefore have **no** `Client` row: that is what a node-issued key looks like
from bot-api's side.
"""

import pytest

from panel_core.xray import gateway as gw

from tests.schema import ensure_schema


REALITY_STREAM = {
    "network": "tcp",
    "security": "reality",
    "realitySettings": {
        "publicKey": "5rBK9zJd0hTfR2Xn7wQm4vPcL1sYbA6eNgUiO3tHkDs",
        "fingerprint": "chrome",
        "serverNames": ["www.microsoft.com"],
        "shortIds": ["0123abcd"],
    },
}


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


@pytest.fixture
def botapi_app(monkeypatch, tmp_path):
    monkeypatch.setenv("PANEL_ROLE", "bot")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/botapi.db"))
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.com")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)

    import importlib

    module = importlib.import_module("panel_core.roles.botapi")
    return module.create_app()


def _seed(app, *, telegram_id, token="statetoken"):
    from panel_core.extensions import db
    from panel_core.models import LinkedPanel, SystemSetting, TelegramUser

    with app.app_context():
        db.session.add(SystemSetting(key="bot_service_token", value=token))
        db.session.add(TelegramUser(telegram_id=telegram_id, sub_token=f"tok-{telegram_id}"))
        db.session.add(
            LinkedPanel(
                id=7,
                name="Amsterdam",
                url="https://node-ams.example.com/secret",
                federation_token="ft",
                enable=True,
                created_at=1_700_000_000_000,
            )
        )
        db.session.commit()


def _snapshot(telegram_id, *, client_id, expiry_time=1_900_000_000_000):
    return {
        "inbounds": [
            {
                "tag": "vless-reality",
                "label": "Amsterdam VLESS",
                "protocol": "vless",
                "port": 443,
                "stream_settings": REALITY_STREAM,
                "clients": [
                    {
                        "id": client_id,
                        "email": f"tg{telegram_id}_vless-reality",
                        "telegram_id": telegram_id,
                        "enable": True,
                        "up": 1024,
                        "down": 2048,
                        "limit_bytes": 0,
                        "expiry_time": expiry_time,
                        "flow": "xtls-rprx-vision",
                    }
                ],
            }
        ]
    }


def _state(app, monkeypatch, telegram_id, snapshot, token="statetoken"):
    import panel_core.services.panel_proxy as panel_proxy

    monkeypatch.setattr(panel_proxy, "get_panel_snapshot", lambda panel_id: snapshot)
    resp = app.test_client().get(
        f"/api/bot-service/users/{telegram_id}/state",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def test_a_node_issued_client_comes_back_with_a_share_link(botapi_app, monkeypatch):
    """The one thing the bot could not do: show a key.

    `_build_share_links` shipped from panel-sub, which panel-botapi did not depend on, so the
    bot's "Show keys" screen fell back to the admin API of a monolith that no longer exists and
    answered "no keys" to users who had them.
    """

    client_id = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
    _seed(botapi_app, telegram_id=4001)

    data = _state(botapi_app, monkeypatch, 4001, _snapshot(4001, client_id=client_id))

    assert len(data["clients"]) == 1
    links = data["clients"][0]["links"]
    assert len(links) == 1, links

    link = links[0]
    assert link.startswith(f"vless://{client_id}@node-ams.example.com:443?")
    assert "security=reality" in link
    assert "flow=xtls-rprx-vision" in link
    assert link.endswith("#Amsterdam%20VLESS")


def test_the_host_comes_from_the_panel_url_not_from_this_hosts_domain(botapi_app, monkeypatch):
    """bot-api is not a node. Its own PANEL_DOMAIN would be a confidently wrong address."""

    monkeypatch.setenv("PANEL_DOMAIN", "bot.example.com")
    _seed(botapi_app, telegram_id=4002)

    data = _state(botapi_app, monkeypatch, 4002, _snapshot(4002, client_id="cccccccc-1111-2222-3333-dddddddddddd"))

    link = data["clients"][0]["links"][0]
    assert "@node-ams.example.com:" in link
    assert "bot.example.com" not in link


def test_a_missing_snapshot_yields_no_client_and_no_link(botapi_app, monkeypatch):
    """A dead cron means an empty subscription, not a broken response."""

    _seed(botapi_app, telegram_id=4003)

    data = _state(botapi_app, monkeypatch, 4003, None)

    assert data["clients"] == []
    assert data["expires_at_ms"] is None


def test_an_unlimited_key_absorbs_a_dated_one_in_the_aggregate(botapi_app, monkeypatch):
    """§49: `0` means "never expires" and is smaller than every date, so `max()` is the wrong rule.

    Wave 3a wrote that rule down once, in `collapse_expiries`, because a plain max() picks the
    dated key and the bot then tells a user with permanent access that it runs out next month.
    This is the third consumer of the same rule after backfill and the provisioning reply.
    """

    _seed(botapi_app, telegram_id=4004)

    snapshot = _snapshot(4004, client_id="eeeeeeee-1111-2222-3333-ffffffffffff", expiry_time=1_900_000_000_000)
    snapshot["inbounds"].append(
        {
            "tag": "vless-forever",
            "label": "Forever",
            "protocol": "vless",
            "port": 8443,
            "stream_settings": REALITY_STREAM,
            "clients": [
                {
                    "id": "99999999-1111-2222-3333-999999999999",
                    "email": "tg4004_vless-forever",
                    "telegram_id": 4004,
                    "enable": True,
                    "up": 0,
                    "down": 0,
                    "limit_bytes": 0,
                    "expiry_time": 0,
                    "flow": "",
                }
            ],
        }
    )

    data = _state(botapi_app, monkeypatch, 4004, snapshot)

    assert len(data["clients"]) == 2
    assert data["expires_at_ms"] == 0, (
        "a permanent key must absorb the dated one; max() would report the dated key's expiry and "
        "the bot would show a user with unlimited access a date"
    )


def test_a_dated_key_alone_still_reports_its_own_date(botapi_app, monkeypatch):
    """The fix must not turn every aggregate into 0 -- that would hide real expiry from everyone."""

    _seed(botapi_app, telegram_id=4005)

    data = _state(
        botapi_app,
        monkeypatch,
        4005,
        _snapshot(4005, client_id="12345678-1111-2222-3333-121212121212", expiry_time=1_888_000_000_000),
    )

    assert data["expires_at_ms"] == 1_888_000_000_000
