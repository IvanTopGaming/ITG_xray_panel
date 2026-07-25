import json
import shutil
import subprocess
import tempfile

import pytest

from panel_core.extensions import db
from panel_core.models import Client, Inbound, TelegramUser


pytestmark = pytest.mark.skipif(shutil.which("sing-box") is None, reason="sing-box binary not installed")


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

VMESS_TLS_STREAM = json.dumps(
    {
        "network": "ws",
        "security": "tls",
        "tlsSettings": {"serverName": "example.com", "alpn": ["h2", "http/1.1"]},
        "wsSettings": {"path": "/vm", "headers": {"Host": "example.com"}},
    }
)

TROJAN_TLS_STREAM = json.dumps(
    {
        "network": "tcp",
        "security": "tls",
        "tlsSettings": {"serverName": "example.com"},
    }
)

SS_STREAM = json.dumps(
    {
        "network": "tcp",
        "security": "none",
        "ssMethod": "chacha20-ietf-poly1305",
        "ssPassword": "serverpassword123",
    }
)


@pytest.fixture
def app(app):
    from panel_core.api import subscription as sub_api

    if "subscription" not in app.blueprints:
        app.register_blueprint(sub_api.bp, url_prefix="/api")
    return app


def _run_singbox_check(config_json):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write(config_json)
        path = fh.name
    proc = subprocess.run(
        ["sing-box", "check", "-c", path],
        capture_output=True,
        text=True,
    )
    return proc


def _seed(tag, port, protocol, stream, *, flow="", uuid="11111111-1111-1111-1111-111111111111"):
    ib = Inbound(tag=tag, port=port, protocol=protocol, stream_settings=stream, label=tag.upper())
    db.session.add(ib)
    db.session.flush()
    c = Client(
        id=uuid,
        email=f"u_{tag}",
        inbound_tag=tag,
        telegram_id=900,
        enable=True,
        flow=flow,
    )
    db.session.add(c)
    if not TelegramUser.query.filter_by(telegram_id=900).first():
        db.session.add(TelegramUser(telegram_id=900, sub_token="tok-900-aaaaaaaaaaaaaaaaaaaaaaaa"))
    db.session.commit()
    return c.id


CASES = [
    ("vless-reality", 443, "vless", REALITY_STREAM, "xtls-rprx-vision", "aaaaaaaa-0000-0000-0000-000000000001"),
    ("vmess-tls", 444, "vmess", VMESS_TLS_STREAM, "", "bbbbbbbb-0000-0000-0000-000000000002"),
    ("trojan-tls", 445, "trojan", TROJAN_TLS_STREAM, "", "cccccccc-0000-0000-0000-000000000003"),
    ("ss-node", 446, "shadowsocks", SS_STREAM, "", "dddddddd-0000-0000-0000-000000000004"),
]


@pytest.mark.parametrize("tag,port,protocol,stream,flow,uuid", CASES)
def test_singbox_single_client_check_passes(app, tag, port, protocol, stream, flow, uuid):
    from panel_core.api.subscription import generate_singbox_config

    with app.app_context():
        cid = _seed(tag, port, protocol, stream, flow=flow, uuid=uuid)
        cfg = generate_singbox_config(cid)
        assert cfg is not None
        proc = _run_singbox_check(cfg)
        assert proc.returncode == 0, f"sing-box check failed for {protocol}:\n{proc.stderr}\n{proc.stdout}\n{cfg}"


@pytest.mark.parametrize("tag,port,protocol,stream,flow,uuid", CASES)
def test_singbox_aggregated_check_passes(app, tag, port, protocol, stream, flow, uuid):
    from panel_core.api.subscription import generate_singbox_config_for_user

    with app.app_context():
        _seed(tag, port, protocol, stream, flow=flow, uuid=uuid)
        cfg = generate_singbox_config_for_user(900)
        assert cfg is not None
        proc = _run_singbox_check(cfg)
        assert proc.returncode == 0, (
            f"sing-box check failed for aggregated {protocol}:\n{proc.stderr}\n{proc.stdout}\n{cfg}"
        )
