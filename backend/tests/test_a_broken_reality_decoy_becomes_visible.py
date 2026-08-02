"""§106: a REALITY inbound that cannot work looked exactly like one that does.

Found on a live stand: an inbound built through the panel, with keys the panel generated, refused
every client. The node logged `REALITY: processed invalid connection` per attempt and the panel
showed a healthy inbound, a live client and a subscription that served fine — the only thing that
did not work was connecting.

Two different causes hide behind that one symptom, and neither was reported:

1. **The decoy itself cannot serve as a REALITY target.** Measured: `www.microsoft.com` fails every
   handshake while `www.google.com`, `www.bing.com`, `www.apple.com` and `www.cloudflare.com`
   succeed, with all five serving TLS 1.3, h2, X25519 and P-256. No probe of the address at save
   time separates them, so the count of refused handshakes travels to the master instead.
2. **The SNI and the reverse proxy disagree.** On :443 Caddy routes by SNI and learns the decoy name
   from `PROXY_DOMAIN`; the inbound's `serverNames` is what the client presents. Two places, one
   value, nothing checking. That one *is* decidable at save time, so it is refused.
"""

from __future__ import annotations

import datetime
import importlib
import json
import time

import jwt as jwt_lib
import pytest

from panel_core.extensions import db, scheduler
from panel_core.models import Admin, SystemSetting
from panel_core.services import reality_health
from panel_core.utils import SECRET_KEY

DECOY = "www.google.com"
PRIVATE_KEY = "mCaQVClTw-CjfMM--qO7og3Dy_GN1HK_QWqrosZjVHA"
SHORT_ID = "bae6b48d0d231227"


def _reset_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)


@pytest.fixture(autouse=True)
def _no_xray_side_effects(monkeypatch):
    monkeypatch.setattr("panel_core.api.inbound.generate_config_file", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr("panel_core.api.inbound.restart_xray_container", lambda *a, **kw: None, raising=False)


@pytest.fixture
def node(monkeypatch, tmp_path):
    from panel_core.xray import gateway as gw

    monkeypatch.setenv("PANEL_ROLE", "worker")
    monkeypatch.setenv("PROXY_DOMAIN", DECOY)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    _reset_scheduler()
    gw.set_xray_gateway(None)
    app = importlib.import_module("panel_core.roles.worker").create_app()
    yield app
    _reset_scheduler()


@pytest.fixture
def headers(node):
    with node.app_context():
        admin = Admin.query.first()
        payload = {
            "user": admin.username,
            "admin_id": admin.id,
            "role": "admin",
            "pwdv": int(admin.password_changed_at or 0),
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
        }
    return {"Authorization": f"Bearer {jwt_lib.encode(payload, SECRET_KEY, algorithm='HS256')}"}


def _payload(sni, *, port=443, tag="vless-reality"):
    return {
        "tag": tag,
        "port": port,
        "protocol": "vless",
        "network": "tcp",
        "security": "reality",
        "realityDest": f"{sni}:443",
        "realitySNI": sni,
        "realityPrivateKey": PRIVATE_KEY,
        "realityShortIds": SHORT_ID,
    }


def test_an_sni_the_proxy_will_not_route_is_refused(node, headers):
    response = node.test_client().post("/api/inbounds", json=_payload("www.microsoft.com"), headers=headers)

    assert response.status_code == 400, (
        f"the inbound saved with an SNI the reverse proxy does not route, so every client would be "
        f"handed to the panel instead of Xray and simply never connect (HTTP {response.status_code})"
    )
    message = response.get_json()["error"]
    assert "www.microsoft.com" in message and DECOY in message, (
        f"the refusal does not name both values, so the admin cannot tell which of the two to change: {message!r}"
    )


def test_the_matching_sni_is_accepted(node, headers):
    response = node.test_client().post("/api/inbounds", json=_payload(DECOY), headers=headers)
    assert response.status_code == 201, f"a correct REALITY inbound was refused: {response.get_json()!r}"


def test_a_port_the_proxy_does_not_touch_is_not_second_guessed(node, headers):
    response = node.test_client().post(
        "/api/inbounds", json=_payload("www.microsoft.com", port=8443, tag="other"), headers=headers
    )
    assert response.status_code == 201, (
        f"only :443 goes through the SNI router; refusing other ports invents a rule that does not "
        f"exist: {response.get_json()!r}"
    )


def test_refused_handshakes_are_counted_within_a_window(node):
    with node.app_context():
        assert reality_health.read_failures()["count"] == 0

        reality_health.record_failures(3)
        assert reality_health.read_failures()["count"] == 3

        reality_health.record_failures(2)
        assert reality_health.read_failures()["count"] == 5, "the count is not cumulative within the window"

        aged = time.time() + reality_health.WINDOW_SECONDS + 1
        assert reality_health.read_failures(now=aged)["count"] == 0, (
            "the window never ages out, so one bad afternoon would warn forever"
        )


def test_a_damaged_counter_reads_as_zero_rather_than_raising(node):
    with node.app_context():
        db.session.add(SystemSetting(key=reality_health.SETTING_KEY, value="not json"))
        db.session.commit()
        assert reality_health.read_failures()["count"] == 0


def test_the_count_travels_to_the_master_in_the_snapshot(node):
    from panel_core.models import FederationConfig

    with node.app_context():
        reality_health.record_failures(7)
        cfg = db.session.get(FederationConfig, 1)
        if cfg is None:
            cfg = FederationConfig(id=1)
            db.session.add(cfg)
        cfg.federation_token = "token-the-master-holds"
        db.session.commit()

    response = node.test_client().get(
        "/api/federation/snapshot", headers={"X-Federation-Token": "token-the-master-holds"}
    )
    assert response.status_code == 200, response.data
    payload = json.loads(response.data)
    assert payload["reality_failures"]["count"] == 7, (
        f"the master cannot see refused handshakes, so the only place the symptom appears is the "
        f"node's own log file: {payload.get('reality_failures')!r}"
    )
