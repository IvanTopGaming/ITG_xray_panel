"""§8.1: the subscription surface exists on one role, and the link points at that role.

Three separate things had to move together, and each one alone would have been a regression:

* the blueprint left master and worker — an unauthenticated endpoint on an admin host, and a
  three-image rebuild for every subscription edit;
* `_try_proxy_sub_to_child` went with it — it fetched `/api/sub/<uuid>` **from the node**, so
  dropping the node's blueprint first would have turned a working path into a 404, and dropping
  the proxy first would have done the same before sub could build the config itself;
* the `PANEL_DOMAIN` fallback left `build_aggregate_sub_url` — it produced a link to the master,
  which now answers nothing at all under `/api/sub/`.
"""

import base64
import json
import pathlib

import pytest

from tests.schema import ensure_schema

REPO = pathlib.Path(__file__).resolve().parents[2]

NODE_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

SNAPSHOT = {
    "inbounds": [
        {
            "tag": "DE-vless",
            "port": 8443,
            "protocol": "vless",
            "label": "Germany",
            "device_limit": 0,
            "stream_settings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "publicKey": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "shortIds": ["abcd1234"],
                    "fingerprint": "chrome",
                    "serverNames": ["google.com"],
                },
            },
            "clients": [
                {
                    "id": NODE_UUID,
                    "email": "tg700_DE-vless",
                    "enable": True,
                    "up": 111,
                    "down": 222,
                    "limit_bytes": 3000,
                    "expiry_time": 1800000000000,
                    "flow": "xtls-rprx-vision",
                    "telegram_id": 700,
                }
            ],
        }
    ]
}


def _build_role(monkeypatch, tmp_path, role, module_name, filename):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", role)
    monkeypatch.setenv("DATABASE_URL", ensure_schema(f"sqlite:///{tmp_path}/{filename}"))
    monkeypatch.chdir(tmp_path)
    gw.set_xray_gateway(None)

    import importlib

    return importlib.import_module(f"panel_core.roles.{module_name}").create_app()


@pytest.fixture
def sub_app(monkeypatch, tmp_path):
    app = _build_role(monkeypatch, tmp_path, "sub", "sub", "sub.db")

    from panel_core.extensions import db
    from panel_core.models import LinkedPanel

    with app.app_context():
        db.session.add(
            LinkedPanel(
                name="de",
                url="https://node1.example.com",
                federation_token="tok",
                enable=True,
                created_at=0,
            )
        )
        db.session.commit()

    monkeypatch.setattr("panel_core.services.panel_proxy.get_panel_snapshot", lambda panel_id: SNAPSHOT)
    monkeypatch.setattr("panel_core.services.sub_cache.get", lambda kind, key: None)
    monkeypatch.setattr("panel_core.services.sub_cache.set", lambda kind, key, value: None)
    return app


def test_the_proxy_to_the_node_is_gone():
    from panel_core.api import subscription

    assert not hasattr(subscription, "_try_proxy_sub_to_child"), (
        "the per-UUID path used to fetch the config from the node over HTTP with timeout=8, so a dead "
        "node stalled a live user's request for eight seconds. It is rebuilt from the snapshot now."
    )

    source = (
        REPO / "backend" / "packages" / "panel-sub" / "src" / "panel_core" / "api" / "subscription.py"
    ).read_text()
    assert "timeout=8" not in source
    assert "import requests" not in source, "no role should reach a node from the subscription path"


@pytest.mark.parametrize("role,module,filename", [("master", "master", "m.db"), ("worker", "worker", "w.db")])
def test_neither_admin_role_answers_the_subscription_routes(monkeypatch, tmp_path, role, module, filename):
    app = _build_role(monkeypatch, tmp_path, role, module, filename)
    try:
        rules = sorted(r.rule for r in app.url_map.iter_rules() if r.rule.startswith("/api/sub"))
        assert rules == [], f"{role} still serves {rules}"

        client = app.test_client()
        assert client.get(f"/api/sub/{NODE_UUID}", headers={"User-Agent": "v2rayNG"}).status_code == 404
        assert client.get("/api/sub/u/anytoken").status_code == 404
    finally:
        from panel_core.extensions import scheduler

        if scheduler.running:
            scheduler.shutdown(wait=False)
        for job in list(scheduler.get_jobs()):
            scheduler.remove_job(job.id)


def test_sub_builds_the_v2ray_config_for_a_node_client_from_the_snapshot(sub_app):
    resp = sub_app.test_client().get(f"/api/sub/{NODE_UUID}", headers={"User-Agent": "v2rayNG/1.9"})

    assert resp.status_code == 200
    decoded = base64.b64decode(resp.data).decode("utf-8")
    assert decoded.startswith("vless://")
    assert NODE_UUID in decoded
    assert "node1.example.com:8443" in decoded
    assert "security=reality" in decoded
    assert "sid=abcd1234" in decoded
    assert "flow=xtls-rprx-vision" in decoded
    assert "Germany" in decoded


def test_sub_builds_clash_for_a_node_client(sub_app):
    """Clash and sing-box worked for node clients through the proxy; they must keep working."""

    import yaml

    resp = sub_app.test_client().get(f"/api/sub/{NODE_UUID}", headers={"User-Agent": "clash-verge/1.0"})

    assert resp.status_code == 200
    assert resp.mimetype == "text/yaml"
    config = yaml.safe_load(resp.data)
    proxies = config["proxies"]
    assert len(proxies) == 1
    assert proxies[0]["server"] == "node1.example.com"
    assert proxies[0]["port"] == 8443
    assert proxies[0]["uuid"] == NODE_UUID


def test_sub_builds_singbox_for_a_node_client(sub_app):
    resp = sub_app.test_client().get(f"/api/sub/{NODE_UUID}", headers={"User-Agent": "sing-box/1.9"})

    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    config = json.loads(resp.data)
    outbound = config["outbounds"][0]
    assert outbound["server"] == "node1.example.com"
    assert outbound["server_port"] == 8443
    assert outbound["uuid"] == NODE_UUID


def test_the_userinfo_header_comes_from_the_snapshot(sub_app):
    """The accepted cost of §8.1: counters are cached, up to 60s stale, instead of live from the node."""

    resp = sub_app.test_client().get(f"/api/sub/{NODE_UUID}", headers={"User-Agent": "v2rayNG/1.9"})

    userinfo = resp.headers.get("subscription-userinfo")
    parts = {p.split("=")[0].strip(): int(p.split("=")[1]) for p in userinfo.split(";")}
    assert parts["upload"] == 111
    assert parts["download"] == 222
    assert parts["total"] == 3000
    assert parts["expire"] == 1800000000
    assert resp.headers.get("profile-title") == "tg700_DE-vless"
    assert "tg700_DE-vless" in resp.headers.get("Content-Disposition")


def test_an_unknown_uuid_is_404_not_an_empty_config(sub_app):
    resp = sub_app.test_client().get("/api/sub/99999999-9999-9999-9999-999999999999")
    assert resp.status_code == 404


def test_a_disabled_node_client_is_404(sub_app, monkeypatch):
    import copy

    disabled = copy.deepcopy(SNAPSHOT)
    disabled["inbounds"][0]["clients"][0]["enable"] = False
    monkeypatch.setattr("panel_core.services.panel_proxy.get_panel_snapshot", lambda panel_id: disabled)

    resp = sub_app.test_client().get(f"/api/sub/{NODE_UUID}")
    assert resp.status_code == 404


def test_the_aggregate_url_has_no_panel_domain_fallback(monkeypatch):
    from panel_core.services.sub_links import build_aggregate_sub_url, build_client_sub_url

    monkeypatch.delenv("SUB_DOMAIN", raising=False)
    monkeypatch.setenv("PANEL_DOMAIN", "panel.example.com")
    monkeypatch.setenv("PANEL_SECRET_PATH", "s3cr3t")

    assert build_aggregate_sub_url("tok") is None, (
        "the fallback produced a link to the master, which serves no /api/sub/* at all since wave 3b. "
        "A missing link is honest; a link that 404s in a browser while client apps still work is not."
    )
    assert build_client_sub_url("some-uuid") is None

    monkeypatch.setenv("SUB_DOMAIN", "sub.example.com")
    assert build_aggregate_sub_url("tok") == "https://sub.example.com/api/sub/u/tok"
    assert build_client_sub_url("some-uuid") == "https://sub.example.com/api/sub/some-uuid"


@pytest.mark.parametrize("compose", ["docker-compose.master.yml", "docker-compose.node.yml"])
def test_the_panel_hosts_demand_the_sub_domain(compose):
    """Not removed from these two hosts: their admin API still builds the links their UI hands out."""

    text = (REPO / compose).read_text()
    assert "SUB_DOMAIN=${SUB_DOMAIN:?" in text, (
        f"{compose} must demand SUB_DOMAIN through `:?`. api/inbound.py builds every client's sub_url "
        f"from it, and there is no fallback left — an empty value means the Dashboard's copy button and "
        f"QR have nowhere to point."
    )
