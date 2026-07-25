import json
import os
import base64
import binascii
import ipaddress
import re
import secrets
from urllib.parse import urlparse
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

X25519_KEY_BYTES = 32
ALLOWED_LOG_LEVELS = {"debug", "info", "warning", "error", "none"}
DEFAULT_GEOIP_URL = (
    os.getenv(
        "XRAY_GEOIP_URL",
        "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
    ).strip()
    or "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"
)
DEFAULT_GEOSITE_URL = (
    os.getenv(
        "XRAY_GEOSITE_URL",
        "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat",
    ).strip()
    or "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"
)
DEFAULT_LOG_LEVEL = os.getenv("XRAY_LOG_LEVEL", "info").strip().lower() or "info"
ALLOWED_ROUTING_RULE_KEYS = {
    "type",
    "domain",
    "ip",
    "port",
    "network",
    "source",
    "protocol",
    "user",
    "inboundTag",
    "outboundTag",
    "balancerTag",
}
TRUTHY_VALUES = {"1", "true", "yes", "on"}
FALSY_VALUES = {"0", "false", "no", "off"}
SS2022_METHOD_KEY_LENGTHS = {
    "2022-blake3-aes-128-gcm": 16,
    "2022-blake3-aes-256-gcm": 32,
    "2022-blake3-chacha20-poly1305": 32,
}
VALID_STREAM_NETWORKS = {
    "tcp",
    "kcp",
    "ws",
    "http",
    "domainsocket",
    "quic",
    "grpc",
    "httpupgrade",
    "splithttp",
    "xhttp",
}
VALID_PACKET_NETWORKS = {"tcp", "udp", "tcp,udp"}
VALID_TLS_ALPN = {"h2", "http/1.1", "http/1.0", "h3"}
VALID_UTLS_FINGERPRINTS = {
    "chrome",
    "firefox",
    "safari",
    "ios",
    "android",
    "edge",
    "360",
    "qq",
    "random",
    "randomized",
}
PACKET_NETWORK_ALIASES = {"udp,tcp": "tcp,udp"}

if DEFAULT_LOG_LEVEL not in ALLOWED_LOG_LEVELS:
    DEFAULT_LOG_LEVEL = "info"


def normalize_xray_log_level(value):
    level = str(value or "").strip().lower()
    if level not in ALLOWED_LOG_LEVELS:
        allowed = ", ".join(sorted(ALLOWED_LOG_LEVELS))
        raise ValueError(f"Invalid Xray log level. Allowed values: {allowed}")
    return level


def normalize_geo_data_url(value, field_name="URL"):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be a valid http(s) URL")
    return raw


def _flag_enabled(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in TRUTHY_VALUES:
        return True
    if raw in FALSY_VALUES:
        return False
    return default


def generate_reality_keys():
    try:
        private_key = x25519.X25519PrivateKey.generate()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return {
            "privateKey": base64.urlsafe_b64encode(private_bytes).decode("utf-8").rstrip("="),
            "publicKey": base64.urlsafe_b64encode(public_bytes).decode("utf-8").rstrip("="),
        }
    except Exception:
        raise


def generate_reality_short_id():

    return secrets.token_hex(8)


def generate_proxy_credentials():
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    password_alphabet = alphabet + "-_.!@#%^&*"
    username = "".join(secrets.choice(alphabet) for _ in range(12))
    password = "".join(secrets.choice(password_alphabet) for _ in range(20))
    return {"username": username, "password": password}


def generate_password(length=24):
    try:
        size = int(length)
    except (TypeError, ValueError):
        size = 24
    if size < 8:
        size = 8
    if size > 128:
        size = 128
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
    return "".join(secrets.choice(alphabet) for _ in range(size))


def normalize_stream_network(value, default="tcp"):
    network = str(value or "").strip().lower()
    if network in VALID_STREAM_NETWORKS:
        return network
    return default


def normalize_packet_network(value, default="tcp"):
    network = str(value or "").strip().lower().replace(" ", "")
    network = PACKET_NETWORK_ALIASES.get(network, network)
    if network in VALID_PACKET_NETWORKS:
        return network
    return default


def stream_supports_vless_flow(stream):
    if not isinstance(stream, dict):
        return False
    return stream.get("network") == "tcp" and stream.get("security") in ("tls", "reality")


def inbound_supports_vless_flow(inbound):
    if getattr(inbound, "protocol", None) != "vless":
        return False
    try:
        stream = json.loads(inbound.stream_settings or "{}")
    except (TypeError, ValueError):
        return False
    return stream_supports_vless_flow(stream)


def is_shadowsocks_2022_method(method):
    normalized = str(method or "").strip().lower()
    return normalized in SS2022_METHOD_KEY_LENGTHS


def _decode_base64_any(value):
    encoded = str(value or "").strip()
    if not encoded:
        return b""
    padded = encoded + ("=" * ((4 - len(encoded) % 4) % 4))
    for decoder in (
        lambda raw: base64.b64decode(raw, validate=True),
        lambda raw: base64.b64decode(raw, altchars=b"-_", validate=True),
    ):
        try:
            decoded = decoder(padded)
        except (ValueError, binascii.Error):
            continue
        if decoded:
            return decoded
    return b""


def normalize_shadowsocks_2022_key(value, method):
    normalized_method = str(method or "").strip().lower()
    if normalized_method not in SS2022_METHOD_KEY_LENGTHS:
        return str(value or "").strip()

    decoded = _decode_base64_any(value)
    if not decoded:
        return ""

    required_len = SS2022_METHOD_KEY_LENGTHS[normalized_method]
    allowed_lengths = {required_len}
    if required_len == 16:
        allowed_lengths.add(32)
    if len(decoded) not in allowed_lengths:
        return ""

    return base64.b64encode(decoded).decode("utf-8")


def generate_shadowsocks_password(method):
    normalized_method = str(method or "").strip().lower()
    if normalized_method in SS2022_METHOD_KEY_LENGTHS:
        key_len = SS2022_METHOD_KEY_LENGTHS[normalized_method]
        return base64.b64encode(secrets.token_bytes(key_len)).decode("utf-8")
    return generate_password(24)


def generate_shadowsocks_user_key(method):
    normalized_method = str(method or "").strip().lower()
    key_len = SS2022_METHOD_KEY_LENGTHS.get(normalized_method, 32)
    return base64.b64encode(secrets.token_bytes(key_len)).decode("utf-8")


def generate_wireguard_keys():
    try:
        private_key = x25519.X25519PrivateKey.generate()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return {
            "privateKey": base64.b64encode(private_bytes).decode("utf-8"),
            "publicKey": base64.b64encode(public_bytes).decode("utf-8"),
        }
    except Exception:
        raise


def _decode_x25519_private_key(key):
    encoded = str(key or "").strip()
    if not encoded:
        return b""

    padded = encoded + ("=" * ((4 - len(encoded) % 4) % 4))
    for decoder in (
        lambda value: base64.b64decode(value, validate=True),
        lambda value: base64.b64decode(value, altchars=b"-_", validate=True),
    ):
        try:
            raw = decoder(padded)
        except (ValueError, binascii.Error):
            continue
        if len(raw) == X25519_KEY_BYTES:
            return raw
    return b""


def _normalize_reality_key(key):
    raw = _decode_x25519_private_key(key)
    if not raw:
        return ""
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _normalize_wireguard_key(key):
    raw = _decode_x25519_private_key(key)
    if not raw:
        return ""
    return base64.b64encode(raw).decode("utf-8")


def _derive_reality_pubkey(private_key):
    try:
        normalized_private = _normalize_reality_key(private_key)
        if not normalized_private:
            return ""

        raw_private = _decode_x25519_private_key(normalized_private)
        if not raw_private:
            return ""

        private_obj = x25519.X25519PrivateKey.from_private_bytes(raw_private)
        public_bytes = private_obj.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.urlsafe_b64encode(public_bytes).decode("utf-8").rstrip("=")
    except (ValueError, TypeError):
        return ""


def _derive_wg_pubkey(private_key):
    try:
        key = _normalize_wireguard_key(private_key)
        if not key:
            return ""
        raw_private = _decode_x25519_private_key(key)
        if not raw_private:
            return ""
        private_obj = x25519.X25519PrivateKey.from_private_bytes(raw_private)
        public_bytes = private_obj.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(public_bytes).decode("utf-8")
    except (ValueError, TypeError):
        return ""


def _validate_short_id(sid):
    if not re.fullmatch(r"[0-9a-fA-F]+", sid):
        raise ValueError(f"Invalid ShortId: {sid}")
    if len(sid) > 16:
        raise ValueError(f"ShortId too long: {sid}")


def _validate_port(port):
    try:
        port_int = int(port)
        if not (1 <= port_int <= 65535):
            raise ValueError
        return port_int
    except (TypeError, ValueError):
        raise ValueError(f"Invalid port: {port}")


def _parse_host_port(value, field_name="fallback_address"):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} required")
    if ":" not in raw:
        raise ValueError(f"{field_name} must be in host:port format")
    host, port_str = raw.rsplit(":", 1)
    host = host.strip()
    if not host:
        raise ValueError(f"{field_name} host is required")
    return host, _validate_port(port_str.strip())


_CERT_PATH_PREFIX = "/etc/xray/"


def _host_is_internal(host):
    h = (host or "").strip().lower()
    if not h:
        return True
    ip = None
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        import socket

        try:
            ip = ipaddress.ip_address(socket.inet_aton(h))
        except (OSError, ValueError):
            ip = None
    if ip is None:
        return h == "localhost" or "." not in h or h.endswith((".local", ".internal", ".localhost"))
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def _validate_reality_dest(value):
    host, port = _parse_host_port(value, "REALITY dest")
    if _host_is_internal(host):
        raise ValueError("REALITY dest must be a public host:port")
    return f"{host}:{port}"


def _validate_cert_path(path, field_name):
    p = str(path or "").strip()
    if not p:
        return p
    real = os.path.realpath(p)
    if real != "/etc/xray" and not real.startswith(_CERT_PATH_PREFIX):
        raise ValueError(f"{field_name} must be a path under /etc/xray")
    return p


def _build_stream_settings(settings_dict):
    protocol = str(settings_dict.get("protocol", "") or "").strip().lower()
    requested_network = settings_dict.get("network", "tcp")
    stream = {
        "network": normalize_stream_network(requested_network),
        "security": settings_dict.get("security", "none"),
    }

    if protocol in ["socks", "http"]:
        stream["network"] = "tcp"
        stream["security"] = "none"
    elif protocol == "shadowsocks" and stream["security"] not in ["none", "tls"]:
        raise ValueError("Shadowsocks security must be none or tls")

    if stream["security"] == "reality":
        if stream["network"] not in ["tcp", "grpc", "xhttp"]:
            stream["network"] = "tcp"

    if stream["network"] == "ws":
        path = settings_dict.get("wsPath", "/")
        if not path.startswith("/"):
            path = "/" + path
        stream["wsSettings"] = {"path": path}
        if settings_dict.get("wsHost"):
            stream["wsSettings"]["headers"] = {"Host": settings_dict.get("wsHost")}

    elif stream["network"] == "grpc":
        sname = settings_dict.get("grpcServiceName", "grpc")
        if not sname:
            raise ValueError("Service Name required")
        stream["grpcSettings"] = {"serviceName": sname}

    elif stream["network"] == "xhttp":
        path = settings_dict.get("wsPath", "/")
        if not path.startswith("/"):
            path = "/" + path
        stream["xhttpSettings"] = {"path": path}
        if settings_dict.get("wsHost"):
            stream["xhttpSettings"]["host"] = settings_dict.get("wsHost")

    elif stream["network"] == "httpupgrade":
        path = settings_dict.get("wsPath", "/")
        stream["httpUpgradeSettings"] = {
            "path": path,
            "host": settings_dict.get("wsHost", ""),
        }

    elif stream["network"] == "splithttp":
        path = settings_dict.get("wsPath", "/")
        stream["splitHttpSettings"] = {
            "path": path,
            "host": settings_dict.get("wsHost", ""),
        }

    if stream["security"] == "tls":
        tls_server_name = str(settings_dict.get("tlsServerName", "") or "").strip()
        raw_tls_alpn = settings_dict.get("tlsAlpn", "")
        tls_cert_file = _validate_cert_path(settings_dict.get("tlsCertFile", ""), "TLS certificate file")
        tls_key_file = _validate_cert_path(settings_dict.get("tlsKeyFile", ""), "TLS key file")
        tls_utls_fingerprint = str(settings_dict.get("tlsUTLSFingerprint", "") or "").strip()

        if bool(tls_cert_file) != bool(tls_key_file):
            raise ValueError("TLS certificate and key file must be provided together")

        if isinstance(raw_tls_alpn, list):
            tls_alpn = [str(item).strip() for item in raw_tls_alpn if str(item).strip()]
        else:
            tls_alpn = [item.strip() for item in str(raw_tls_alpn or "").split(",") if item.strip()]

        for alpn in tls_alpn:
            if alpn.lower() not in VALID_TLS_ALPN:
                raise ValueError(f'Invalid ALPN "{alpn}" — allowed: {", ".join(sorted(VALID_TLS_ALPN))}')
        if tls_utls_fingerprint and tls_utls_fingerprint.lower() not in VALID_UTLS_FINGERPRINTS:
            raise ValueError(
                f'Invalid TLS fingerprint "{tls_utls_fingerprint}" — allowed: {", ".join(sorted(VALID_UTLS_FINGERPRINTS))}'
            )

        tls_settings = {}
        if tls_server_name:
            tls_settings["serverName"] = tls_server_name
        if tls_alpn:
            tls_settings["alpn"] = tls_alpn
        if tls_cert_file and tls_key_file:
            tls_settings["certificates"] = [
                {
                    "certificateFile": tls_cert_file,
                    "keyFile": tls_key_file,
                }
            ]
        if tls_utls_fingerprint:
            tls_settings["_utlsFingerprint"] = tls_utls_fingerprint
        if tls_settings:
            stream["tlsSettings"] = tls_settings

    if stream["security"] == "reality":
        raw_sids = settings_dict.get("realityShortIds", "")
        sids = [s.strip() for s in raw_sids.split(",") if s.strip()] if raw_sids else [""]
        for s in sids:
            if s:
                _validate_short_id(s)

        pk = _normalize_reality_key(settings_dict.get("realityPrivateKey", ""))
        if not pk:
            raise ValueError("Invalid REALITY private key")

        public_key = (settings_dict.get("realityPublicKey", "") or "").strip()
        if public_key:
            public_key = _normalize_reality_key(public_key)
            if not public_key:
                raise ValueError("Invalid REALITY public key")
        else:
            public_key = _derive_reality_pubkey(pk)

        reality_fp = str(settings_dict.get("realityFingerprint", "chrome") or "chrome").strip()
        if reality_fp.lower() not in VALID_UTLS_FINGERPRINTS:
            raise ValueError(
                f'Invalid REALITY fingerprint "{reality_fp}" — allowed: {", ".join(sorted(VALID_UTLS_FINGERPRINTS))}'
            )

        stream["realitySettings"] = {
            "show": False,
            "dest": _validate_reality_dest(settings_dict.get("realityDest", "www.google.com:443")),
            "xver": 0,
            "serverNames": [settings_dict.get("realitySNI", "www.google.com")],
            "privateKey": pk,
            "shortIds": sids,
            "publicKey": public_key,
            "fingerprint": reality_fp,
            "spiderX": settings_dict.get("realitySpiderX", ""),
        }

    if protocol == "shadowsocks":
        ss_method = str(settings_dict.get("ssMethod", "2022-blake3-aes-128-gcm") or "").strip()
        ss_password = str(settings_dict.get("ssPassword", "") or "").strip()
        if is_shadowsocks_2022_method(ss_method):
            normalized_ss_password = normalize_shadowsocks_2022_key(ss_password, ss_method)
            if not normalized_ss_password:
                raise ValueError("Invalid Shadowsocks 2022 server password for selected method")
            ss_password = normalized_ss_password
        stream["ssNetwork"] = normalize_packet_network(settings_dict.get("ssNetwork", requested_network))
        stream["ssMethod"] = ss_method
        stream["ssPassword"] = ss_password

    if protocol == "wireguard":
        secret_key = (settings_dict.get("wgSecretKey") or settings_dict.get("realityPrivateKey") or "").strip()
        public_key = (settings_dict.get("wgPublicKey") or "").strip()

        if secret_key:
            secret_key = _normalize_wireguard_key(secret_key)
            if not secret_key:
                raise ValueError("Invalid WireGuard private key")

        if not secret_key:
            generated = generate_wireguard_keys()
            secret_key = generated["privateKey"]
            if not public_key:
                public_key = generated["publicKey"]

        if public_key:
            public_key = _normalize_wireguard_key(public_key)
            if not public_key:
                raise ValueError("Invalid WireGuard public key")

        if not public_key:
            public_key = _derive_wg_pubkey(secret_key)

        if not public_key:
            raise ValueError("WireGuard public key required")

        stream["wgSecretKey"] = secret_key
        stream["wgPublicKey"] = public_key
        raw_mtu = settings_dict.get("wgMTU")
        if raw_mtu not in [None, ""]:
            try:
                mtu = int(raw_mtu)
            except (TypeError, ValueError):
                raise ValueError("Invalid WireGuard MTU")
            if not (576 <= mtu <= 9000):
                raise ValueError("WireGuard MTU must be between 576 and 9000")
            stream["wgMTU"] = mtu

    if protocol in ["socks", "http"]:
        auth_user = str(settings_dict.get("authUser", "") or "").strip()
        auth_pass = str(settings_dict.get("authPass", "") or "").strip()
        if bool(auth_user) != bool(auth_pass):
            raise ValueError("Username and password must be provided together")
        stream["authUser"] = auth_user
        stream["authPass"] = auth_pass

    return stream
