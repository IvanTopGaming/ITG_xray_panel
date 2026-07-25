import importlib
import importlib.util

import pytest

ENGINE_EXPORTS = [
    "generate_config_file",
    "restart_xray_container",
    "stream_xray_logs",
    "update_geo_db",
]

ENGINE_CONSTANTS = [
    "CONFIG_PATH",
    "LOCK_PATH",
    "CANDIDATE_PATH",
    "XRAY_CONTAINER_NAME",
    "XRAY_BIN",
    "ACCESS_LOG_PATH",
    "ERROR_LOG_PATH",
    "LOG_TAIL_LINES",
]


@pytest.mark.parametrize("name", ENGINE_EXPORTS)
def test_engine_exposes_runtime_function(name):
    mod = importlib.import_module("panel_core.xray.engine")
    assert callable(getattr(mod, name))


@pytest.mark.parametrize("name", ENGINE_CONSTANTS)
def test_engine_exposes_constant(name):
    mod = importlib.import_module("panel_core.xray.engine")
    assert getattr(mod, name) is not None


def test_old_services_xray_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("panel_core.services.xray")
