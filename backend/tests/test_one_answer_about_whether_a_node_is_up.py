"""N10: the admin and the user were told different things about the same node.

`LinkedPanel.status` in Postgres is written by the cron host, and only when the status *changes*.
`panel:<id>:status` in the shared Redis is written by the same host every 10 seconds and expires
after 120. Two stores, one fact, different staleness — and until now two readers, one on each.

With the poller dead the two diverge in the worst direction: wave 5d taught the master's Panels card
to answer `stale` when the live marker is gone, so an admin sees amber and goes looking. The
subscription page kept reading the Postgres row, which still says `online` and will say `online`
forever, so the user is told every node is up while nobody has contacted any of them for hours.

Both now read `get_panel_liveness`. The mapping is deliberately not identical, because the audiences
are not: the admin is shown `stale`, which is a statement about the *pipeline* and is what they can
act on; the user is shown a node as up unless the poller actually said `offline`, because "we have
stopped polling" is not a fact about their VPN and there is nothing they could do with it.

The Postgres column stays as the fallback for the one case Redis cannot answer — a panel that has
never been polled at all, or a shared tier that is unreachable.
"""

from __future__ import annotations

import importlib

import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Client, Inbound, LinkedPanel, TelegramUser

from tests.schema import ensure_schema


SUB_TOKEN = "wave7-liveness-token"
NODE_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

SNAPSHOT = {
    "app_version": "2.4.14",
    "inbounds": [
        {
            "tag": "DE-vless",
            "label": "Germany",
            "port": 8443,
            "protocol": "vless",
            "stream_settings": {"network": "tcp", "security": "none"},
            "clients": [
                {
                    "id": NODE_UUID,
                    "email": "tg700_DE-vless",
                    "enable": True,
                    "up": 1,
                    "down": 2,
                    "limit_bytes": 0,
                    "expiry_time": 1900000000000,
                    "telegram_id": 700,
                }
            ],
        }
    ],
}


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    try:
        scheduler.remove_all_jobs()
    except Exception:
        pass


@pytest.fixture
def sub_app(monkeypatch, tmp_path):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "sub")
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/sub.db"))
    monkeypatch.setenv("SUB_DOMAIN", "sub.example.com")
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)
    app = importlib.import_module("panel_core.roles.sub").create_app()

    with app.app_context():
        db.session.add(Inbound(tag="local", port=443, protocol="vless", stream_settings="{}"))
        db.session.add(TelegramUser(telegram_id=700, sub_token=SUB_TOKEN, language="ru"))
        db.session.add(Client(id=NODE_UUID, email="tg700_local", inbound_tag="local", telegram_id=700, enable=True))
        # `status` is what the cron host last wrote, on a change. It is never refreshed afterwards.
        db.session.add(
            LinkedPanel(
                name="de",
                url="https://node1.example.com",
                federation_token="tok",
                enable=True,
                created_at=0,
                status="online",
            )
        )
        db.session.commit()

    monkeypatch.setattr("panel_core.services.panel_proxy.get_panel_snapshot", lambda pid: SNAPSHOT)
    monkeypatch.setattr("panel_core.services.sub_cache.get", lambda kind, key: None)
    monkeypatch.setattr("panel_core.services.sub_cache.set", lambda kind, key, value: None)
    yield app
    _reset_scheduler()


def _nodes(sub_app, monkeypatch, liveness):
    monkeypatch.setattr("panel_core.services.panel_proxy.get_panel_liveness", lambda pid: liveness)
    body = sub_app.test_client().get(f"/api/sub/u/{SUB_TOKEN}/info").get_json()
    return [n for n in body["nodes"] if n["name"] == "Germany"]


def test_a_polled_and_healthy_node_reads_as_up(sub_app, monkeypatch):
    assert _nodes(sub_app, monkeypatch, ("online", 1))[0]["online"] is True


def test_a_node_the_poller_calls_offline_reads_as_down(sub_app, monkeypatch):
    """The Postgres row still says `online` here — if that is what wins, this test is the proof."""

    assert _nodes(sub_app, monkeypatch, ("offline", 1))[0]["online"] is False


def test_a_dead_poller_does_not_turn_into_a_promise_about_the_node(sub_app, monkeypatch):
    """`stale` means "nobody is polling", not "the node is down" — the user is not shown a guess as fact."""

    assert _nodes(sub_app, monkeypatch, ("stale", 1))[0]["online"] is True


def test_the_postgres_row_is_only_the_fallback(sub_app, monkeypatch):
    """A panel never polled, or a shared tier that cannot answer: fall back rather than invent."""

    assert _nodes(sub_app, monkeypatch, (None, None))[0]["online"] is True

    with sub_app.app_context():
        LinkedPanel.query.first().status = "offline"
        db.session.commit()
    assert _nodes(sub_app, monkeypatch, (None, None))[0]["online"] is False


def test_both_readers_take_the_same_source():
    """Textual, because the divergence was two call sites reading two different stores.

    A future edit that puts `panel.status` back on the user-facing path restores exactly the split
    this closes, and it would look locally correct in both files.
    """

    from tests.import_graph import source_path

    page = source_path("api/subscription.py").read_text()
    assert "get_panel_liveness" in page, "the subscription page stopped consulting the live marker"
    assert page.count('(panel.status or "").lower() == "online"') == 0, (
        "the page reads the Postgres column as its primary source again — that column is written on "
        "change only, so with the poller dead it reports every node up forever."
    )

    admin = source_path("api/panels.py").read_text()
    assert "get_panel_liveness" in admin
