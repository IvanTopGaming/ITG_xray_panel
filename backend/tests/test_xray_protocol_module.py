import importlib

import pytest

PURE_NAMES = [
    "normalize_xray_log_level",
    "normalize_geo_data_url",
    "normalize_stream_network",
    "normalize_packet_network",
    "stream_supports_vless_flow",
    "inbound_supports_vless_flow",
    "is_shadowsocks_2022_method",
    "normalize_shadowsocks_2022_key",
    "generate_shadowsocks_password",
    "generate_shadowsocks_user_key",
    "generate_reality_keys",
    "generate_reality_short_id",
    "generate_proxy_credentials",
    "generate_password",
    "generate_wireguard_keys",
]

PURE_CONSTANTS = [
    "DEFAULT_GEOIP_URL",
    "DEFAULT_GEOSITE_URL",
    "DEFAULT_LOG_LEVEL",
    "ALLOWED_LOG_LEVELS",
    "VALID_STREAM_NETWORKS",
    "VALID_PACKET_NETWORKS",
    "VALID_TLS_ALPN",
    "VALID_UTLS_FINGERPRINTS",
    "PACKET_NETWORK_ALIASES",
    "TRUTHY_VALUES",
    "FALSY_VALUES",
    "ALLOWED_ROUTING_RULE_KEYS",
]


@pytest.mark.parametrize("name", PURE_NAMES)
def test_pure_function_present(name):
    mod = importlib.import_module("panel_core.xray.protocol")
    assert callable(getattr(mod, name))


@pytest.mark.parametrize("name", PURE_CONSTANTS)
def test_pure_constant_present(name):
    mod = importlib.import_module("panel_core.xray.protocol")
    assert getattr(mod, name) is not None


def test_settings_module_exposes_get_system_settings():
    mod = importlib.import_module("panel_core.xray.settings")
    assert callable(getattr(mod, "get_system_settings"))


def test_flow_compatibility_rule_unchanged():
    from panel_core.xray.protocol import stream_supports_vless_flow

    assert stream_supports_vless_flow({"network": "tcp", "security": "tls"}) is True
    assert stream_supports_vless_flow({"network": "tcp", "security": "reality"}) is True
    assert stream_supports_vless_flow({"network": "tcp", "security": "none"}) is False
    assert stream_supports_vless_flow({"network": "ws", "security": "tls"}) is False
