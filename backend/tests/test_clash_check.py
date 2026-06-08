"""Empirical Clash.Meta config conformance: validate generated single-client AND
aggregated Clash configs against the real `mihomo` (Clash.Meta) binary.

Skipped automatically when the binary is not on PATH (e.g. on a laptop / CI
without the core installed). On the dev server `mihomo` v1.19.x is on PATH.

`mihomo -t -f <config.yaml> -d <workdir>` parses/validates the config and exits
non-zero on an invalid config. A shared workdir is used so any geo data mihomo
downloads (for the GEOIP rule) is cached across cases instead of re-fetched.
"""

import json
import shutil
import subprocess

import pytest

from app.extensions import db
from app.models import Client, Inbound, TelegramUser


pytestmark = pytest.mark.skipif(shutil.which("mihomo") is None, reason="mihomo binary not installed")


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
    from app.api import subscription as sub_api

    if "subscription" not in app.blueprints:
        app.register_blueprint(sub_api.bp, url_prefix="/api")
    return app


@pytest.fixture(scope="session")
def mihomo_workdir(tmp_path_factory):
    """Persistent working dir so geo data mihomo may download is reused across cases."""
    return tmp_path_factory.mktemp("mihomo-wd")


def _run_mihomo_check(config_yaml, workdir, tmp_path):
    path = tmp_path / "clash.yaml"
    path.write_text(config_yaml)
    proc = subprocess.run(
        ["mihomo", "-t", "-f", str(path), "-d", str(workdir)],
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
def test_clash_single_client_check_passes(app, mihomo_workdir, tmp_path, tag, port, protocol, stream, flow, uuid):
    from app.api.subscription import generate_clash_config

    with app.app_context():
        cid = _seed(tag, port, protocol, stream, flow=flow, uuid=uuid)
        cfg = generate_clash_config(cid)
        assert cfg is not None
        proc = _run_mihomo_check(cfg, mihomo_workdir, tmp_path)
        assert proc.returncode == 0, f"mihomo check failed for {protocol}:\n{proc.stderr}\n{proc.stdout}\n{cfg}"


@pytest.mark.parametrize("tag,port,protocol,stream,flow,uuid", CASES)
def test_clash_aggregated_check_passes(app, mihomo_workdir, tmp_path, tag, port, protocol, stream, flow, uuid):
    from app.api.subscription import generate_clash_config_for_user

    with app.app_context():
        _seed(tag, port, protocol, stream, flow=flow, uuid=uuid)
        cfg = generate_clash_config_for_user(900)
        assert cfg is not None
        proc = _run_mihomo_check(cfg, mihomo_workdir, tmp_path)
        assert proc.returncode == 0, (
            f"mihomo check failed for aggregated {protocol}:\n{proc.stderr}\n{proc.stdout}\n{cfg}"
        )
