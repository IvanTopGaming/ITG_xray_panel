import pytest

from panel_core.xray import gateway as gw


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
