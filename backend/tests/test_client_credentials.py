import json

import pytest

from panel_core.extensions import db
from panel_core.models import Client, Inbound, Outbound


def seed_credentials():
    from panel_core.models import ClientCredential

    db.session.add(Inbound(tag="aliases", protocol="vless", port=12444, stream_settings="{}"))
    client = Client(
        id="primary-key",
        email="primary-email",
        inbound_tag="aliases",
        enable=True,
        up=0,
        down=0,
        traffic_generation="cycle-1",
        preferred_outbound="direct",
    )
    db.session.add(client)
    db.session.flush()
    db.session.add(ClientCredential(id="old-key", client_id=client.id, email="old-email"))
    db.session.commit()
    return client


def test_alias_resolves_and_deletion_cascades(app):
    from panel_core.models import ClientCredential
    from panel_core.services.client_credentials import credentials_for, resolve_client

    client = seed_credentials()
    assert resolve_client("old-key") is client
    assert [row.id for row in credentials_for(client)] == ["primary-key", "old-key"]
    db.session.delete(client)
    db.session.commit()
    assert ClientCredential.query.count() == 0
    assert resolve_client("old-key") is None


def sample(client, primary, alias):
    from panel_core.services.runtime_identity import build_runtime_email

    return (
        "epoch",
        {
            f"user>>>{build_runtime_email('aliases', email)}>>>traffic>>>uplink": value
            for email, value in (("primary-email", primary), ("old-email", alias))
        },
        {client.id: client.traffic_generation},
    )


def test_alias_counters_share_usage_and_cycle_without_double_count(app):
    from panel_core.services.traffic_store import settle_client_traffic, start_traffic_cycle

    client = seed_credentials()
    first = sample(client, 10, 30)
    assert settle_client_traffic(client, sample=first) == (40, 0)
    assert settle_client_traffic(client, sample=first) == (0, 0)
    start_traffic_cycle(client, sample=first, generation="cycle-2")
    assert client.up == 0
    assert settle_client_traffic(client, sample=sample(client, 15, 37)) == (12, 0)
    db.session.commit()
    assert client.up == 12


@pytest.mark.parametrize("protocol", ["vless", "vmess", "trojan", "shadowsocks"])
def test_config_keeps_alias_auth_and_routing(app, monkeypatch, protocol):
    from panel_core.xray import engine
    from panel_core.services.runtime_identity import build_runtime_email

    client = seed_credentials()
    db.session.add(Outbound(tag="direct", protocol="freedom", enable=True, settings="{}", stream_settings="{}"))
    client.inbound.protocol = protocol
    client.inbound.stream_settings = json.dumps({"ssMethod": "aes-128-gcm", "ssPassword": "server-password"})
    db.session.commit()
    monkeypatch.setattr(engine, "_validate_xray_config", lambda *args, **kwargs: None)
    engine.generate_config_file()
    with open(engine.CONFIG_PATH) as stream:
        config = json.load(stream)
    inbound = next(row for row in config["inbounds"] if row["tag"] == "aliases")
    key = "id" if protocol in {"vless", "vmess"} else "password"
    assert {row[key] for row in inbound["settings"]["clients"]} == {"primary-key", "old-key"}
    user_rules = [row for row in config["routing"]["rules"] if "user" in row]
    assert {build_runtime_email("aliases", "primary-email"), build_runtime_email("aliases", "old-email")} <= {
        email for row in user_rules for email in row["user"]
    }


def test_alias_access_log_updates_canonical_client(app, tmp_path, monkeypatch):
    from panel_core.models import DomainStat
    from panel_core.services import stats
    from panel_core.services.runtime_identity import build_runtime_email

    client = seed_credentials()
    access_log = tmp_path / "access.log"
    access_log.touch()
    identity = build_runtime_email("aliases", "old-email")
    monkeypatch.setattr(stats, "ACCESS_LOG_PATH", str(access_log))
    monkeypatch.setattr(
        stats,
        "_read_log_chunk",
        lambda *args: f"2026/10/03 10:00:00 192.0.2.1:1234 accepted tcp:example.org:443 email: {identity}\n",
    )
    stats._parse_access_logs_logic()
    assert json.loads(client.source_ips) == ["192.0.2.1"]
    assert client.last_seen > 0
    assert DomainStat.query.one().client_email == client.email
