import importlib
import inspect

import pytest


@pytest.mark.parametrize("name", ["get_channel", "_api_add_user_grpc", "_api_remove_user_grpc"])
def test_grpc_client_exposes(name):
    mod = importlib.import_module("panel_core.xray.grpc_client")
    assert callable(getattr(mod, name))


def test_stats_module_no_longer_imports_grpc_directly():
    source = inspect.getsource(importlib.import_module("panel_core.services.stats"))
    assert "\nimport grpc" not in source
    assert "from app.proxyman" not in source
    assert "from app.stats" not in source
    assert "from common.protocol" not in source
    assert "from proxy.vless" not in source


def test_grpc_client_owns_the_protobuf_namespaces():
    source = inspect.getsource(importlib.import_module("panel_core.xray.grpc_client"))
    assert "from app.proxyman.command import" in source
    assert "from app.stats.command import" in source
