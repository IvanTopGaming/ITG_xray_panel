import pytest

from panel_core.extensions import db
from panel_core.models import Client, Inbound, AccessEntitlement


def duplicate_clients(protocol="vless", **overrides):
    db.session.add(Inbound(tag="merge", protocol=protocol, port=15001, stream_settings="{}"))
    for index, expiry in enumerate((2000000000000, 2100000000000)):
        values = dict(
            id=f"key-{index}",
            email=f"email-{index}",
            telegram_id=42,
            tariff_id=index + 1,
            inbound_tag="merge",
            expiry_time=expiry,
            limit_bytes=100 * (index + 1),
            up=0,
            down=0,
            enable=True,
            provisioning_key=f"42:{index + 1}:merge",
        )
        if index:
            values.update(overrides)
        db.session.add(Client(**values))
    db.session.commit()
    if "expiry_time" in overrides and overrides["expiry_time"] is None:
        from sqlalchemy import update

        db.session.execute(update(Client).where(Client.id == "key-1").values(expiry_time=None))
        db.session.commit()


def test_dry_run_reports_without_mutation_or_credentials(app):
    from panel_core.services.shared_client_migration import migration_report

    duplicate_clients()
    report = migration_report()
    assert report["groups"][0]["status"] == "ready"
    assert "key-0" not in str(report)
    assert Client.query.count() == 2
    assert AccessEntitlement.query.count() == 0


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"preferred_outbound": "route-2"}, "conflicting_preferred_outbound"),
        ({"manual_disabled": True}, "conflicting_manual_disabled"),
        ({"enable": False}, "conflicting_enable"),
        ({"up": 1}, "existing_usage_requires_review"),
        ({"expiry_time": None}, "damaged_expiry"),
    ],
)
def test_ambiguous_groups_remain_unchanged(app, overrides, reason):
    from panel_core.services.shared_client_migration import migration_report, apply_migration

    duplicate_clients(**overrides)
    report = migration_report()
    assert reason in report["groups"][0]["reasons"]
    assert apply_migration(report)["merged_groups"] == 0
    assert Client.query.count() == 2


def test_offline_merge_preserves_dates_keys_sources_and_is_repeatable(app):
    from panel_core.models import ClientCredential
    from panel_core.services.client_credentials import resolve_client
    from panel_core.services.shared_client_migration import migration_report, apply_migration

    duplicate_clients()
    result = apply_migration(migration_report())
    assert result["merged_groups"] == 1
    assert Client.query.count() == 1
    canonical = Client.query.one()
    assert resolve_client("key-0") is canonical
    assert resolve_client("key-1") is canonical
    assert canonical.expiry_time == 2100000000000
    assert {row.expires_at_ms for row in AccessEntitlement.query.all()} == {2000000000000, 2100000000000}
    assert {row.client_id for row in AccessEntitlement.query.all()} == {canonical.id}
    assert ClientCredential.query.one().original_data["client"]["id"] == "key-1"
    assert apply_migration(migration_report())["merged_groups"] == 0


def test_report_cannot_be_applied_after_state_changes(app):
    from panel_core.services.shared_client_migration import migration_report, apply_migration

    duplicate_clients()
    report = migration_report()
    Client.query.first().limit_bytes = 1000
    db.session.commit()
    with pytest.raises(ValueError, match="changed"):
        apply_migration(report)
    assert Client.query.count() == 2


@pytest.mark.parametrize("protocol", ["wireguard", "http", "socks"])
def test_unsupported_protocols_are_reported(app, protocol):
    from panel_core.services.shared_client_migration import migration_report

    duplicate_clients(protocol)
    assert "unsupported_protocol" in migration_report()["groups"][0]["reasons"]


def test_cli_only_writes_explicit_output_copy(app, tmp_path, monkeypatch, capsys):
    import json
    import sqlite3
    import sys
    from migrate_shared_clients import main

    duplicate_clients()
    source, output = tmp_path / "source.db", tmp_path / "output.db"
    with sqlite3.connect(source) as target:
        db.session.connection().connection.driver_connection.backup(target)
    before = source.read_bytes()
    monkeypatch.setattr(sys, "argv", ["migrate_shared_clients.py", str(source)])
    main()
    assert json.loads(capsys.readouterr().out)["groups"][0]["status"] == "ready"
    assert source.read_bytes() == before
    monkeypatch.setattr(sys, "argv", ["migrate_shared_clients.py", str(source), "--apply-to", str(output)])
    main()
    assert json.loads(capsys.readouterr().out)["result"]["merged_groups"] == 1
    assert source.read_bytes() == before
    with sqlite3.connect(output) as migrated:
        assert migrated.execute("SELECT count(*) FROM client").fetchone()[0] == 1
        assert migrated.execute("SELECT count(*) FROM client_credential").fetchone()[0] == 1
    assert output.stat().st_mode & 0o777 == 0o600


def test_existing_sources_with_manual_quota_override_require_review(app):
    from panel_core.services.shared_client_migration import migration_report

    duplicate_clients()
    db.session.add(
        AccessEntitlement(
            source_id="pay:old",
            operation_id="pay:old",
            telegram_id=42,
            tariff_id=1,
            inbound_tag="merge",
            client_id="key-0",
            expires_at_ms=2000000000000,
            limit_bytes=999,
            enabled=True,
            revoked=False,
        )
    )
    db.session.commit()
    assert "manual_quota_requires_review" in migration_report()["groups"][0]["reasons"]
