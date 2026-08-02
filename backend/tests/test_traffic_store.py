import ast

from tests.import_graph import HEAVY_ROOTS_DOC, XRAY_SEAM_MODULES, heavy_root, source_path

STORE = source_path("services/traffic_store.py")


def _imported_names(path):
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_traffic_store_exists():
    assert STORE.exists(), "traffic_store.py must exist for the master to import without protobuf stubs"


def test_traffic_store_imports_nothing_heavy():
    names = _imported_names(STORE)
    offenders = {name for name in names if name in XRAY_SEAM_MODULES or heavy_root(name)}
    assert offenders == set(), (
        f"traffic_store must stay free of Xray/gRPC imports, found: {sorted(offenders)}\n\n{HEAVY_ROOTS_DOC}"
    )


def test_traffic_store_exposes_the_master_facing_api():
    from panel_core.services import traffic_store

    for name in (
        "_ten_min_bucket",
        "_upsert_snapshot",
        "_upsert_domain_stat",
        "cleanup_old_domain_stats",
        "cleanup_stats_job",
        "reset_user_traffic",
        "reset_inbound_traffic",
        "bulk_delete_users",
    ):
        assert hasattr(traffic_store, name), f"traffic_store is missing {name}"


def test_stats_facade_still_exposes_moved_names():
    from panel_core.services import stats, traffic_store

    for name in ("_ten_min_bucket", "cleanup_stats_job", "reset_user_traffic", "bulk_delete_users"):
        assert getattr(stats, name) is getattr(traffic_store, name), f"{name} must be re-exported, not re-implemented"


def test_reset_inbound_traffic_goes_through_the_gateway(app, db):
    from unittest.mock import MagicMock, patch

    from panel_core.extensions import db as _db
    from panel_core.models import Client, Inbound
    from panel_core.services.traffic_store import reset_inbound_traffic

    _db.session.add(Inbound(tag="NL-vless", protocol="vless", port=10101, stream_settings="{}", up=900, down=900))
    _db.session.add(Client(id="i1", email="n1", inbound_tag="NL-vless", up=400, down=500, enable=True, expiry_time=0))
    _db.session.commit()

    gateway = MagicMock()
    gateway.has_local_xray.return_value = True
    with patch("panel_core.services.traffic_store.get_xray_gateway", return_value=gateway):
        reset_inbound_traffic("NL-vless")

    gateway.reset_inbound_counters.assert_called_once_with("NL-vless")
    gateway.reset_user_counters.assert_called_once()
    ib = Inbound.query.filter_by(tag="NL-vless").first()
    client = Client.query.filter_by(inbound_tag="NL-vless", email="n1").first()
    assert (ib.up, ib.down) == (0, 0)
    assert (client.up, client.down) == (0, 0)


def test_reset_inbound_traffic_skips_xray_without_local_instance(app, db):
    from unittest.mock import MagicMock, patch

    from panel_core.extensions import db as _db
    from panel_core.models import Client, Inbound
    from panel_core.services.traffic_store import reset_inbound_traffic

    _db.session.add(Inbound(tag="NL-vless", protocol="vless", port=10101, stream_settings="{}", up=900, down=900))
    _db.session.add(Client(id="i2", email="n2", inbound_tag="NL-vless", up=400, down=500, enable=True, expiry_time=0))
    _db.session.commit()

    gateway = MagicMock()
    gateway.has_local_xray.return_value = False
    with patch("panel_core.services.traffic_store.get_xray_gateway", return_value=gateway):
        reset_inbound_traffic("NL-vless")

    gateway.reset_inbound_counters.assert_not_called()
    gateway.reset_user_counters.assert_not_called()
    ib = Inbound.query.filter_by(tag="NL-vless").first()
    client = Client.query.filter_by(inbound_tag="NL-vless", email="n2").first()
    assert (ib.up, ib.down) == (0, 0)
    assert (client.up, client.down) == (0, 0)
