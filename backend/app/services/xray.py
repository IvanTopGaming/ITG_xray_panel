import json
import os
import docker
import base64
import binascii
import re
import secrets
import requests
import time
import logging
from urllib.parse import urlparse
from collections import deque
from filelock import FileLock, Timeout
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from app.models import (
    Inbound,
    Outbound,
    Balancer,
    RoutingProfile,
    Client,
    SystemSetting,
)
from app.extensions import db
from app.services.runtime_identity import build_runtime_email, parse_runtime_email

CONFIG_PATH = "/etc/xray/config.json"
LOCK_PATH = "/etc/xray/config.lock"
XRAY_CONTAINER_NAME = "xray-core"
ACCESS_LOG_PATH = "/var/log/xray/access.log"
ERROR_LOG_PATH = "/var/log/xray/error.log"
LOG_TAIL_LINES = 300
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
logger = logging.getLogger(__name__)
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


def _get_system_setting_value(key, default_value):
    try:
        item = SystemSetting.query.filter_by(key=key).first()
        if item and str(item.value or "").strip():
            return str(item.value).strip()
    except Exception:
        logger.debug("Failed to read system setting '%s', using default", key)
    return default_value


def get_system_settings():
    log_level_raw = _get_system_setting_value("xray_log_level", DEFAULT_LOG_LEVEL)
    geoip_url_raw = _get_system_setting_value("geoip_url", DEFAULT_GEOIP_URL)
    geosite_url_raw = _get_system_setting_value("geosite_url", DEFAULT_GEOSITE_URL)

    try:
        xray_log_level = normalize_xray_log_level(log_level_raw)
    except ValueError:
        xray_log_level = DEFAULT_LOG_LEVEL

    try:
        geoip_url = normalize_geo_data_url(geoip_url_raw, "GeoIP URL")
    except ValueError:
        geoip_url = normalize_geo_data_url(DEFAULT_GEOIP_URL, "GeoIP URL")

    try:
        geosite_url = normalize_geo_data_url(geosite_url_raw, "GeoSite URL")
    except ValueError:
        geosite_url = normalize_geo_data_url(DEFAULT_GEOSITE_URL, "GeoSite URL")

    return {
        "xrayLogLevel": xray_log_level,
        "geoipUrl": geoip_url,
        "geositeUrl": geosite_url,
    }


def restart_xray_container():
    try:
        client = docker.from_env()
        container = client.containers.get(XRAY_CONTAINER_NAME)
        container.restart()
    except docker.errors.DockerException as e:
        logger.error("Docker restart error: %s", e)
        raise RuntimeError("Failed to restart Xray container") from e


def _safe_decode(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _ensure_log_file(log_file):
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        if not os.path.exists(log_file):
            with open(log_file, "a", encoding="utf-8"):
                pass
    except OSError as e:
        logger.warning("Failed to ensure log file %s: %s", log_file, e)


def _tail_log_lines(log_file, lines=LOG_TAIL_LINES):
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as file_obj:
            return [line.rstrip("\r\n") for line in deque(file_obj, maxlen=lines) if line.strip()]
    except OSError:
        return []


def stream_xray_logs(tail_lines=LOG_TAIL_LINES):
    for log_file in [ERROR_LOG_PATH, ACCESS_LOG_PATH]:
        _ensure_log_file(log_file)

    try:
        client = docker.from_env()
        container = client.containers.get(XRAY_CONTAINER_NAME)
        container_tail = container.logs(stdout=True, stderr=True, tail=tail_lines)
        for raw_line in _safe_decode(container_tail).splitlines():
            line = raw_line.strip()
            if line:
                yield f"[CONTAINER] {line}"
    except docker.errors.DockerException as e:
        logger.debug("Container log tail unavailable: %s", e)

    for line in _tail_log_lines(ERROR_LOG_PATH, tail_lines):
        yield f"[ERROR] {line}"
    for line in _tail_log_lines(ACCESS_LOG_PATH, tail_lines):
        yield f"[ACCESS] {line}"

    try:
        with (
            open(ERROR_LOG_PATH, "r", encoding="utf-8", errors="replace") as f_error,
            open(ACCESS_LOG_PATH, "r", encoding="utf-8", errors="replace") as f_access,
        ):
            f_error.seek(0, os.SEEK_END)
            f_access.seek(0, os.SEEK_END)

            while True:
                has_new_line = False

                error_line = f_error.readline()
                if error_line:
                    has_new_line = True
                    yield f"[ERROR] {error_line.rstrip()}"

                access_line = f_access.readline()
                if access_line:
                    has_new_line = True
                    yield f"[ACCESS] {access_line.rstrip()}"

                if not has_new_line:
                    time.sleep(0.2)
    except Exception as e:
        yield f"[SYSTEM] Log stream error: {str(e)}"


def update_geo_db():
    settings = get_system_settings()
    urls = {
        "geoip.dat": settings["geoipUrl"],
        "geosite.dat": settings["geositeUrl"],
    }
    for filename, url in urls.items():
        path = f"/etc/xray/{filename}"
        try:
            with requests.get(url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
        except (requests.RequestException, OSError) as exc:
            raise RuntimeError(f"Failed to update {filename}") from exc
    restart_xray_container()


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
    # Xray REALITY accepts up to 16 hex chars, generate the maximum length by default.
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
        # Xray's Go implementation accepts 32-byte keys for AES-128 methods.
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


def _normalize_fallback_dest(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return _validate_port(raw)
    host, port = _parse_host_port(raw)
    return f"{host}:{port}"


# NOTE: keep flatten_stream_settings (below this function) in sync with this builder.
def _build_stream_settings(settings_dict):
    protocol = str(settings_dict.get("protocol", "") or "").strip().lower()
    requested_network = settings_dict.get("network", "tcp")
    stream = {
        "network": normalize_stream_network(requested_network),
        "security": settings_dict.get("security", "none"),
    }

    if protocol in ["socks", "http"]:
        # SOCKS/HTTP inbounds in this panel are configured as plain TCP listeners with optional auth.
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
        tls_cert_file = str(settings_dict.get("tlsCertFile", "") or "").strip()
        tls_key_file = str(settings_dict.get("tlsKeyFile", "") or "").strip()
        tls_utls_fingerprint = str(settings_dict.get("tlsUTLSFingerprint", "") or "").strip()

        if bool(tls_cert_file) != bool(tls_key_file):
            raise ValueError("TLS certificate and key file must be provided together")

        if isinstance(raw_tls_alpn, list):
            tls_alpn = [str(item).strip() for item in raw_tls_alpn if str(item).strip()]
        else:
            tls_alpn = [item.strip() for item in str(raw_tls_alpn or "").split(",") if item.strip()]

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
            # Stored for client config/link generation, not passed into Xray core config.
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

        stream["realitySettings"] = {
            "show": False,
            "dest": settings_dict.get("realityDest", "www.google.com:443"),
            "xver": 0,
            "serverNames": [settings_dict.get("realitySNI", "www.google.com")],
            "privateKey": pk,
            "shortIds": sids,
            "publicKey": public_key,
            "fingerprint": settings_dict.get("realityFingerprint", "chrome"),
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


def flatten_stream_settings(stream, protocol):
    """Inverse of _build_stream_settings: produce the flat-key dict that the
    POST/PUT /api/inbounds endpoint accepts, from a stored Inbound.stream_settings JSON.

    Used by the master → node inbound config sync. Keep in sync with
    _build_stream_settings above.

    Raises ValueError when the inbound uses local TLS certificate files — those paths
    won't exist on a remote node, so syncing them would silently break the node's xray.
    """
    if not isinstance(stream, dict):
        stream = {}

    protocol = str(protocol or "").strip().lower()
    flat = {
        "network": stream.get("network", "tcp"),
        "security": stream.get("security", "none"),
    }

    ws_settings = stream.get("wsSettings") or {}
    xhttp_settings = stream.get("xhttpSettings") or {}
    http_upgrade_settings = stream.get("httpUpgradeSettings") or {}
    split_http_settings = stream.get("splitHttpSettings") or {}
    grpc_settings = stream.get("grpcSettings") or {}
    tls_settings = stream.get("tlsSettings") or {}
    reality_settings = stream.get("realitySettings") or {}

    flat["wsPath"] = (
        ws_settings.get("path")
        or xhttp_settings.get("path")
        or http_upgrade_settings.get("path")
        or split_http_settings.get("path")
        or "/"
    )
    flat["wsHost"] = (
        (ws_settings.get("headers") or {}).get("Host")
        or xhttp_settings.get("host")
        or http_upgrade_settings.get("host")
        or split_http_settings.get("host")
        or ""
    )
    flat["grpcServiceName"] = grpc_settings.get("serviceName", "grpc")

    if isinstance(reality_settings, dict):
        server_names = reality_settings.get("serverNames") or []
        short_ids = reality_settings.get("shortIds") or []
        reality_private = (reality_settings.get("privateKey") or "").strip()
        reality_public = (reality_settings.get("publicKey") or "").strip()
        if reality_private and not reality_public:
            derived = _derive_reality_pubkey(reality_private)
            if derived:
                reality_public = derived
        flat["realityDest"] = reality_settings.get("dest", "www.google.com:443")
        flat["realitySNI"] = server_names[0] if server_names else "www.google.com"
        flat["realityPrivateKey"] = reality_private
        flat["realityPublicKey"] = reality_public
        flat["realityShortIds"] = ",".join([s for s in short_ids if s])
        flat["realityFingerprint"] = reality_settings.get("fingerprint", "chrome")
        flat["realitySpiderX"] = reality_settings.get("spiderX", "")

    if isinstance(tls_settings, dict):
        flat["tlsServerName"] = str(tls_settings.get("serverName", "") or "")
        raw_alpn = tls_settings.get("alpn", [])
        if isinstance(raw_alpn, list):
            flat["tlsAlpn"] = ",".join([str(item).strip() for item in raw_alpn if str(item).strip()])
        elif isinstance(raw_alpn, str):
            flat["tlsAlpn"] = raw_alpn
        else:
            flat["tlsAlpn"] = ""
        flat["tlsUTLSFingerprint"] = str(tls_settings.get("_utlsFingerprint", "") or "")
        certs = tls_settings.get("certificates") or []
        first_cert = certs[0] if isinstance(certs, list) and certs else {}
        if not isinstance(first_cert, dict):
            first_cert = {}
        cert_file = str(first_cert.get("certificateFile", "") or "").strip()
        key_file = str(first_cert.get("keyFile", "") or "").strip()
        if cert_file or key_file:
            raise ValueError("inbound uses local TLS cert files; cannot sync to remote nodes")
        flat["tlsCertFile"] = ""
        flat["tlsKeyFile"] = ""

    if protocol == "shadowsocks":
        flat["ssMethod"] = stream.get("ssMethod", "")
        flat["ssPassword"] = stream.get("ssPassword", "")
        flat["ssNetwork"] = stream.get("ssNetwork", stream.get("network", "tcp"))

    if protocol == "wireguard":
        flat["wgSecretKey"] = stream.get("wgSecretKey", "")
        flat["wgPublicKey"] = stream.get("wgPublicKey", "")
        wg_mtu = stream.get("wgMTU", "")
        flat["wgMTU"] = wg_mtu if wg_mtu not in [None, ""] else ""

    if protocol in ("socks", "http"):
        flat["authUser"] = stream.get("authUser", "")
        flat["authPass"] = stream.get("authPass", "")

    return flat


def generate_config_file():
    lock = FileLock(LOCK_PATH, timeout=5)
    try:
        with lock:
            system_settings = get_system_settings()
            inbounds_db = Inbound.query.all()
            inbounds_json = [
                {
                    "tag": "api",
                    "port": 10085,
                    "listen": "0.0.0.0",
                    "protocol": "dokodemo-door",
                    "settings": {"address": "127.0.0.1"},
                }
            ]

            outbounds_db = Outbound.query.all()
            outbounds_json = []

            proxy_outbounds = []
            observed_tags = []

            for ob in outbounds_db:
                is_system_outbound = ob.tag in ["direct", "block", "api"]
                if not is_system_outbound and not _flag_enabled(getattr(ob, "enable", True), True):
                    continue
                outbounds_json.append(
                    {
                        "tag": ob.tag,
                        "protocol": ob.protocol,
                        "settings": json.loads(ob.settings),
                        "streamSettings": json.loads(ob.stream_settings),
                        "mux": json.loads(ob.mux) if ob.mux else {},
                    }
                )

                if not is_system_outbound:
                    observed_tags.append(ob.tag)
                    proxy_outbounds.append(ob.tag)
            known_outbound_tags = {item["tag"] for item in outbounds_json}

            balancers_db = Balancer.query.all()
            balancers_json = []
            balancer_tags = set()
            for bal in balancers_db:
                if not _flag_enabled(getattr(bal, "enable", True), True):
                    continue
                selector = json.loads(bal.selector) if bal.selector else []
                if not isinstance(selector, list):
                    selector = []
                normalized_selector = [
                    str(item).strip()
                    for item in selector
                    if str(item).strip() and str(item).strip() in known_outbound_tags
                ]
                if not normalized_selector:
                    continue
                balancer_tags.add(bal.tag)
                bal_obj = {
                    "tag": bal.tag,
                    "selector": normalized_selector,
                    "strategy": {"type": bal.strategy},
                }
                fallback_tag = (bal.fallback_tag or "").strip()
                if fallback_tag and fallback_tag in known_outbound_tags:
                    bal_obj["fallbackTag"] = fallback_tag
                balancers_json.append(bal_obj)

            routing_rules = [{"inboundTag": ["api"], "outboundTag": "api", "type": "field"}]

            clients_by_pref = {}
            all_clients = Client.query.filter(
                Client.preferred_outbound.isnot(None), Client.preferred_outbound != ""
            ).all()

            for c in all_clients:
                if c.enable:
                    tag = c.preferred_outbound
                    if tag not in clients_by_pref:
                        clients_by_pref[tag] = []
                    clients_by_pref[tag].append(build_runtime_email(c.inbound_tag, c.email))

            for tag, emails in clients_by_pref.items():
                rule = {
                    "type": "field",
                    "user": emails,
                }
                if tag in balancer_tags:
                    rule["balancerTag"] = tag
                elif tag in known_outbound_tags:
                    rule["outboundTag"] = tag
                else:
                    continue
                routing_rules.append(rule)

            for ib in inbounds_db:
                stream_settings = json.loads(ib.stream_settings)
                requested_network = str(stream_settings.get("network", "tcp") or "tcp").strip().lower()
                stream_settings["network"] = normalize_stream_network(requested_network)
                if stream_settings.get("security") == "tls":
                    tls_settings = stream_settings.get("tlsSettings", {})
                    if isinstance(tls_settings, dict):
                        tls_settings = dict(tls_settings)
                        tls_settings.pop("_utlsFingerprint", None)
                        tls_settings.pop("fingerprint", None)
                        if not tls_settings:
                            stream_settings.pop("tlsSettings", None)
                        else:
                            stream_settings["tlsSettings"] = tls_settings

                if stream_settings.get("security") == "reality":
                    reality_settings = stream_settings.get("realitySettings", {})
                    if not isinstance(reality_settings, dict):
                        raise ValueError(f"Invalid REALITY settings in inbound '{ib.tag}'")

                    normalized_pk = _normalize_reality_key(reality_settings.get("privateKey", ""))
                    if not normalized_pk:
                        raise ValueError(f"Invalid REALITY private key in inbound '{ib.tag}'")

                    reality_settings["privateKey"] = normalized_pk
                    normalized_pub = _normalize_reality_key(reality_settings.get("publicKey", ""))
                    if not normalized_pub:
                        normalized_pub = _derive_reality_pubkey(normalized_pk)
                    reality_settings["publicKey"] = normalized_pub
                    stream_settings["realitySettings"] = reality_settings

                if ib.protocol == "wireguard":
                    secret_key = _normalize_wireguard_key(stream_settings.get("wgSecretKey", ""))
                    if not secret_key:
                        raise ValueError(f"Invalid WireGuard private key in inbound '{ib.tag}'")

                    stream_settings["wgSecretKey"] = secret_key

                    public_key = _normalize_wireguard_key(stream_settings.get("wgPublicKey", ""))
                    if not public_key:
                        public_key = _derive_wg_pubkey(secret_key)
                    if not public_key:
                        raise ValueError(f"Invalid WireGuard public key in inbound '{ib.tag}'")
                    stream_settings["wgPublicKey"] = public_key
                settings = {}

                if ib.protocol in ["vless", "vmess"]:
                    active_clients = [
                        {
                            "id": c.id,
                            "email": build_runtime_email(c.inbound_tag, c.email),
                            "flow": c.flow if c.flow else "",
                            "level": 0,
                        }
                        for c in ib.clients
                        if c.enable
                    ]
                    settings = {"clients": active_clients, "decryption": "none"}
                elif ib.protocol == "trojan":
                    active_clients = [
                        {
                            "password": c.id,
                            "email": build_runtime_email(c.inbound_tag, c.email),
                            "level": 0,
                        }
                        for c in ib.clients
                        if c.enable
                    ]
                    settings = {"clients": active_clients}
                elif ib.protocol == "shadowsocks":
                    method = stream_settings.get("ssMethod", "2022-blake3-aes-128-gcm")
                    server_pass = stream_settings.get("ssPassword", "")
                    if is_shadowsocks_2022_method(method):
                        normalized_server_pass = normalize_shadowsocks_2022_key(server_pass, method)
                        if normalized_server_pass:
                            server_pass = normalized_server_pass
                    clients_list = []
                    for c in ib.clients:
                        if c.enable:
                            client_password = c.id
                            if is_shadowsocks_2022_method(method):
                                normalized_client_password = normalize_shadowsocks_2022_key(client_password, method)
                                if normalized_client_password:
                                    client_password = normalized_client_password
                            clients_list.append(
                                {
                                    "password": client_password,
                                    "email": build_runtime_email(c.inbound_tag, c.email),
                                }
                            )

                    settings = {
                        "method": method,
                        "password": server_pass,
                        "network": normalize_packet_network(stream_settings.get("ssNetwork", requested_network)),
                        "clients": clients_list,
                    }
                elif ib.protocol == "wireguard":
                    secret_key = stream_settings.get("wgSecretKey", "")
                    peers = []
                    ip_suffix = 2
                    for c in ib.clients:
                        if c.enable:
                            pub_key = _derive_wg_pubkey(c.id)
                            if pub_key:
                                peers.append(
                                    {
                                        "publicKey": pub_key,
                                        "allowedIPs": [f"172.19.0.{ip_suffix}/32"],
                                    }
                                )
                                ip_suffix += 1
                    settings = {
                        "secretKey": secret_key,
                        "peers": peers,
                        "kernelMode": False,
                    }
                    raw_mtu = stream_settings.get("wgMTU")
                    try:
                        mtu = int(raw_mtu)
                    except (TypeError, ValueError):
                        mtu = 0
                    if mtu > 0:
                        settings["mtu"] = mtu
                elif ib.protocol in ["socks", "http"]:
                    auth_user = str(stream_settings.get("authUser", "") or "").strip()
                    auth_pass = str(stream_settings.get("authPass", "") or "").strip()
                    has_auth = bool(auth_user and auth_pass)
                    if ib.protocol == "socks":
                        settings = {
                            "auth": "password" if has_auth else "noauth",
                            "udp": True,
                        }
                        if has_auth:
                            settings["accounts"] = [
                                {
                                    "user": auth_user,
                                    "pass": auth_pass,
                                }
                            ]
                    else:
                        settings = {
                            "allowTransparent": False,
                        }
                        if has_auth:
                            settings["accounts"] = [
                                {
                                    "user": auth_user,
                                    "pass": auth_pass,
                                }
                            ]
                else:
                    raise ValueError(f"Unsupported inbound protocol '{ib.protocol}'")

                if ib.fallback_address and ib.protocol in ["vless", "trojan"]:
                    fallback_dest = _normalize_fallback_dest(ib.fallback_address)
                    settings["fallbacks"] = [
                        {
                            "dest": fallback_dest,
                            "xver": 0,
                        }
                    ]

                conf = {
                    "tag": ib.tag,
                    "port": ib.port,
                    "protocol": ib.protocol,
                    "settings": settings,
                    "sniffing": {
                        "enabled": True,
                        "destOverride": ["http", "tls", "quic"],
                        "routeOnly": True,
                    },
                }
                if ib.protocol != "wireguard":
                    stream_settings_for_xray = dict(stream_settings)
                    for extra_key in [
                        "ssMethod",
                        "ssPassword",
                        "ssNetwork",
                        "authUser",
                        "authPass",
                        "wgSecretKey",
                        "wgPublicKey",
                        "wgMTU",
                    ]:
                        stream_settings_for_xray.pop(extra_key, None)
                    conf["streamSettings"] = stream_settings_for_xray
                inbounds_json.append(conf)

                if ib.routing_profile_id:
                    profile = db.session.get(RoutingProfile, ib.routing_profile_id)
                    if profile:
                        if not _flag_enabled(getattr(profile, "enable", True), True):
                            continue
                        p_rules = json.loads(profile.rules)
                        for rule in p_rules:
                            if not isinstance(rule, dict):
                                continue
                            if not _flag_enabled(rule.get("enabled", True), True):
                                continue

                            new_rule = {
                                k: v
                                for k, v in rule.items()
                                if k in ALLOWED_ROUTING_RULE_KEYS and v not in [None, "", []]
                            }
                            if "user" in new_rule:
                                raw_users = (
                                    new_rule["user"] if isinstance(new_rule["user"], list) else [new_rule["user"]]
                                )
                                encoded_users = []
                                for raw_user in raw_users:
                                    user_str = str(raw_user).strip()
                                    if not user_str:
                                        continue
                                    existing_inbound, _ = parse_runtime_email(user_str)
                                    if existing_inbound:
                                        encoded_users.append(user_str)
                                    else:
                                        encoded_users.append(build_runtime_email(ib.tag, user_str))
                                if encoded_users:
                                    new_rule["user"] = encoded_users
                                else:
                                    del new_rule["user"]
                            if "inboundTag" in new_rule and not isinstance(new_rule["inboundTag"], list):
                                new_rule["inboundTag"] = [str(new_rule["inboundTag"])]
                            if "inboundTag" not in new_rule or not new_rule["inboundTag"]:
                                new_rule["inboundTag"] = [ib.tag]
                            else:
                                new_rule["inboundTag"] = [str(item) for item in new_rule["inboundTag"] if item]
                                if ib.tag not in new_rule["inboundTag"]:
                                    new_rule["inboundTag"].append(ib.tag)
                            new_rule["type"] = "field"

                            target = new_rule.get("outboundTag") or new_rule.get("balancerTag")
                            if target in balancer_tags:
                                new_rule["balancerTag"] = target
                                if "outboundTag" in new_rule:
                                    del new_rule["outboundTag"]
                            elif target in known_outbound_tags:
                                new_rule["outboundTag"] = target
                                if "balancerTag" in new_rule:
                                    del new_rule["balancerTag"]
                            else:
                                continue
                            routing_rules.append(new_rule)

            if proxy_outbounds:
                system_balancer_tag = "system_auto_balancer"

                if system_balancer_tag not in balancer_tags:
                    balancers_json.append(
                        {
                            "tag": system_balancer_tag,
                            "selector": proxy_outbounds,
                            "strategy": {"type": "random"},
                        }
                    )

                routing_rules.append(
                    {
                        "type": "field",
                        "network": "tcp,udp",
                        "balancerTag": system_balancer_tag,
                    }
                )

            observatory = {}
            if observed_tags:
                observatory = {
                    "subjectSelector": observed_tags,
                    "probeUrl": "https://www.google.com/generate_204",
                    "probeInterval": "5s",
                }

            full_config = {
                "log": {
                    "loglevel": system_settings["xrayLogLevel"],
                    "access": ACCESS_LOG_PATH,
                    "error": ERROR_LOG_PATH,
                },
                "stats": {},
                "api": {
                    "tag": "api",
                    "services": ["HandlerService", "LoggerService", "StatsService"],
                },
                "policy": {
                    "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
                    "system": {
                        "statsInboundUplink": True,
                        "statsInboundDownlink": True,
                        "statsOutboundUplink": True,
                        "statsOutboundDownlink": True,
                    },
                },
                "inbounds": inbounds_json,
                "routing": {
                    "domainStrategy": "IPIfNonMatch",
                    "rules": routing_rules,
                    "balancers": balancers_json,
                },
                "outbounds": outbounds_json,
                "observatory": observatory,
            }

            temp = CONFIG_PATH + ".tmp"
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(full_config, f, indent=2)
            os.rename(temp, CONFIG_PATH)
    except Timeout:
        raise Exception("Could not acquire lock")
