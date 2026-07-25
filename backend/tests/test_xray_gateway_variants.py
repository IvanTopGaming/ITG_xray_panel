import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from panel_core.xray import gateway as gw
from tests.import_graph import SRC

FORBIDDEN_MODULES = [
    "panel_core.xray.engine",
    "panel_core.xray.grpc_client",
    "grpc",
    "docker",
    "filelock",
]

_ISOLATION_PROBE = """
import importlib
import json
import sys
import types

src, forbidden = sys.argv[1], json.loads(sys.argv[2])

pkg = types.ModuleType("panel_core")
pkg.__path__ = [src]
sys.modules["panel_core"] = pkg

gateway = importlib.import_module("panel_core.xray.gateway")

CALLS = [
    ("apply_config", ()),
    ("restart", ()),
    ("add_user", ("tag", object())),
    ("remove_user", ("tag", "a@b")),
    ("stream_logs", (10,)),
    ("update_geo", ()),
]


def leaked():
    return sorted(name for name in forbidden if name in sys.modules)


def exercise(instance, calls, consume):
    for name, args in calls:
        try:
            result = getattr(instance, name)(*args)
            if consume and name == "stream_logs":
                list(result)
        except Exception:
            pass
    return leaked()


report = {"after_import": leaked()}
report["null"] = exercise(gateway.NullXrayGateway(), CALLS, True)
report["remote"] = exercise(gateway.RemoteXrayGateway(), CALLS, True)
report["control"] = exercise(gateway.LocalXrayGateway(), [("apply_config", ())], False)

print(json.dumps(report))
"""


def _run_isolation_probe():
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_PROBE, str(SRC), json.dumps(FORBIDDEN_MODULES)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"the gateway isolation probe crashed:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_null_gateway_satisfies_protocol():
    assert isinstance(gw.NullXrayGateway(), gw.XrayGateway)


def test_remote_gateway_satisfies_protocol():
    assert isinstance(gw.RemoteXrayGateway(), gw.XrayGateway)


def test_null_gateway_operations_are_noops():
    null = gw.NullXrayGateway()
    assert null.apply_config() is None
    assert null.apply_config(validate=False) is None
    assert null.restart() is None
    assert null.update_geo() is None
    assert null.add_user("tag", object()) is True
    assert null.remove_user("tag", "a@b") is True
    assert list(null.stream_logs(10)) == []


@pytest.mark.parametrize(
    "gateway_class,expected",
    [
        (gw.LocalXrayGateway, True),
        (gw.NullXrayGateway, False),
        (gw.RemoteXrayGateway, False),
    ],
    ids=["local", "null", "remote"],
)
def test_has_local_xray_reports_whether_a_local_instance_exists(gateway_class, expected):
    assert gateway_class().has_local_xray() is expected


def test_facade_has_local_xray_follows_the_installed_gateway():
    import panel_core.xray as facade

    gw.set_xray_gateway(gw.RemoteXrayGateway())
    assert facade.has_local_xray() is False

    gw.set_xray_gateway(gw.LocalXrayGateway())
    assert facade.has_local_xray() is True

    gw.set_xray_gateway(None)


def test_remote_gateway_config_operations_are_noops():
    remote = gw.RemoteXrayGateway()
    assert remote.apply_config() is None
    assert remote.apply_config(validate=False) is None
    assert remote.restart() is None
    assert remote.update_geo() is None


@pytest.mark.parametrize(
    "call",
    [
        lambda g: g.add_user("tag", object()),
        lambda g: g.remove_user("tag", "a@b"),
        lambda g: list(g.stream_logs(10)),
    ],
)
def test_remote_gateway_rejects_local_only_operations(call):
    with pytest.raises(gw.LocalXrayUnavailable):
        call(gw.RemoteXrayGateway())


def test_local_xray_unavailable_is_a_runtime_error():
    assert issubclass(gw.LocalXrayUnavailable, RuntimeError)


def test_remote_gateway_error_names_the_operation():
    with pytest.raises(gw.LocalXrayUnavailable) as excinfo:
        gw.RemoteXrayGateway().add_user("tag", object())
    assert "add_user" in str(excinfo.value)


def test_remote_gateway_stream_logs_raises_eagerly_not_on_first_iteration():
    with pytest.raises(gw.LocalXrayUnavailable):
        gw.RemoteXrayGateway().stream_logs(10)


def test_new_gateways_never_load_local_xray_machinery():
    report = _run_isolation_probe()
    assert report["after_import"] == []
    assert report["null"] == []
    assert report["remote"] == []


def test_the_isolation_probe_is_not_vacuous():
    report = _run_isolation_probe()
    assert "panel_core.xray.engine" in report["control"]


def test_local_gateway_resets_user_counters_via_grpc():
    from panel_core.xray.gateway import LocalXrayGateway

    with patch("panel_core.xray.grpc_client.reset_user_counters") as reset:
        LocalXrayGateway().reset_user_counters("DE-vless", "u1", "DE-vless>>>u1")

    reset.assert_called_once_with("DE-vless", "u1", "DE-vless>>>u1")


def test_local_gateway_resets_inbound_counters_via_grpc():
    from panel_core.xray.gateway import LocalXrayGateway

    with patch("panel_core.xray.grpc_client.reset_inbound_counters") as reset:
        LocalXrayGateway().reset_inbound_counters("DE-vless")

    reset.assert_called_once_with("DE-vless")


def test_null_and_remote_gateways_ignore_counter_resets():
    from panel_core.xray.gateway import NullXrayGateway, RemoteXrayGateway

    for gateway in (NullXrayGateway(), RemoteXrayGateway()):
        assert gateway.reset_user_counters("DE-vless", "u1", "DE-vless>>>u1") is None
        assert gateway.reset_inbound_counters("DE-vless") is None


def test_gateway_protocol_covers_counter_resets():
    from panel_core.xray.gateway import LocalXrayGateway, NullXrayGateway, RemoteXrayGateway, XrayGateway

    for gateway in (LocalXrayGateway(), NullXrayGateway(), RemoteXrayGateway()):
        assert isinstance(gateway, XrayGateway)
