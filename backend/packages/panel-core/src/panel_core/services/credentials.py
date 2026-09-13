import json
import secrets
import uuid

from panel_core.xray.protocol import generate_shadowsocks_user_key, generate_wireguard_keys, is_shadowsocks_2022_method


def generate_client_credentials(inbound):
    if inbound.protocol in ("vless", "vmess"):
        return str(uuid.uuid4())
    if inbound.protocol == "wireguard":
        return generate_wireguard_keys()["privateKey"]
    if inbound.protocol == "shadowsocks":
        settings = inbound.stream_settings or {}
        if isinstance(settings, str):
            settings = json.loads(settings)
        method = settings.get("ssMethod", "aes-256-gcm")
        if is_shadowsocks_2022_method(method):
            return generate_shadowsocks_user_key(method)
        return secrets.token_urlsafe(24)
    if inbound.protocol == "trojan":
        return secrets.token_urlsafe(24)
    raise ValueError("inbound_protocol_does_not_support_clients")
