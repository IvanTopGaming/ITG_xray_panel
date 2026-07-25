import json
import os
import docker
import subprocess
import hashlib
import ipaddress
import requests
import time
import logging
from collections import deque
from filelock import FileLock, Timeout
from panel_core.models import (
    Inbound,
    Outbound,
    Balancer,
    RoutingProfile,
    Client,
)
from panel_core.extensions import db
from panel_core.services.runtime_identity import build_runtime_email, parse_runtime_email
from panel_core.xray.protocol import (
    ALLOWED_ROUTING_RULE_KEYS,
    _derive_reality_pubkey,
    _derive_wg_pubkey,
    _flag_enabled,
    _normalize_reality_key,
    _normalize_wireguard_key,
    _parse_host_port,
    _validate_port,
    is_shadowsocks_2022_method,
    normalize_packet_network,
    normalize_shadowsocks_2022_key,
    normalize_stream_network,
    stream_supports_vless_flow,
)
from panel_core.xray.settings import get_system_settings

CONFIG_PATH = "/etc/xray/config.json"
LOCK_PATH = "/etc/xray/config.lock"
XRAY_CONTAINER_NAME = "xray-core"
XRAY_BIN = "/usr/local/bin/xray"
VALIDATE_TIMEOUT_S = 30
VALIDATE_ATTEMPTS = 2
_CONFIG_BASE, _CONFIG_EXT = os.path.splitext(CONFIG_PATH)
CANDIDATE_PATH = f"{_CONFIG_BASE}.candidate{_CONFIG_EXT}"
ACCESS_LOG_PATH = "/var/log/xray/access.log"
ERROR_LOG_PATH = "/var/log/xray/error.log"
LOG_TAIL_LINES = 300
logger = logging.getLogger(__name__)


def _extract_reason(output):
    text = output.decode("utf-8", "replace") if isinstance(output, (bytes, bytearray)) else str(output or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        low = ln.lower()
        if "failed to start" in low or "failed to" in low or "infra/conf" in low:
            return (ln.split(" > ")[-1].strip() if " > " in ln else ln)[:300]
    if lines:
        return lines[-1][:300]
    return "Xray could not parse the configuration"


def _validate_xray_config(candidate_path):

    if not os.path.exists(XRAY_BIN):
        logger.warning("Config validation skipped — xray binary not bundled at %s", XRAY_BIN)
        return

    last_timeout = None
    for attempt in range(1, VALIDATE_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                [XRAY_BIN, "run", "-test", "-c", candidate_path],
                capture_output=True,
                timeout=VALIDATE_TIMEOUT_S,
                env={**os.environ, "XRAY_LOCATION_ASSET": "/etc/xray"},
            )
        except subprocess.TimeoutExpired as e:
            last_timeout = e
            logger.warning(
                "Xray config validation timed out after %ss (attempt %d/%d)",
                VALIDATE_TIMEOUT_S,
                attempt,
                VALIDATE_ATTEMPTS,
            )
            continue

        if result.returncode == 0:
            return
        raise ValueError("Xray rejected the config: " + _extract_reason(result.stderr or result.stdout))

    raise ValueError("Could not validate the config (Xray validation timed out)") from last_timeout


def restart_xray_container():
    try:
        client = docker.from_env()
        container = client.containers.get(XRAY_CONTAINER_NAME)
        container.restart()
        try:
            client.containers.get("xray-egress").restart()
        except docker.errors.NotFound:
            pass
        except docker.errors.DockerException as e:
            logger.warning("xray-egress restart failed (non-fatal): %s", e)
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


def _normalize_fallback_dest(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return _validate_port(raw)
    host, port = _parse_host_port(raw)
    return f"{host}:{port}"


def _wg_peer_ip(client_id, used):

    base = int.from_bytes(hashlib.sha256(str(client_id).encode()).digest()[:4], "big") % 65532
    for probe in range(65532):
        offset = 2 + ((base + probe) % 65532)
        if offset not in used:
            used.add(offset)
            hi, lo = divmod(offset, 256)
            return f"172.19.{hi}.{lo}/32"
    return None


def generate_config_file(validate=True):
    t0 = time.monotonic()
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

            observed_tags = []

            for ob in outbounds_db:
                is_system_outbound = ob.tag in ["direct", "block", "api"]
                if not is_system_outbound and not _flag_enabled(getattr(ob, "enable", True), True):
                    continue
                ob_json = {
                    "tag": ob.tag,
                    "protocol": ob.protocol,
                    "settings": json.loads(ob.settings),
                    "streamSettings": json.loads(ob.stream_settings),
                    "mux": json.loads(ob.mux) if ob.mux else {},
                }
                if getattr(ob, "send_through", None):
                    ob_json["sendThrough"] = ob.send_through
                    try:
                        ver = ipaddress.ip_address(ob.send_through).version
                        if "domainStrategy" not in ob_json["settings"]:
                            ob_json["settings"]["domainStrategy"] = "UseIPv4" if ver == 4 else "UseIPv6"
                    except ValueError:
                        pass
                outbounds_json.append(ob_json)

                if not is_system_outbound:
                    observed_tags.append(ob.tag)
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
                    flow_allowed = ib.protocol == "vless" and stream_supports_vless_flow(stream_settings)
                    active_clients = [
                        {
                            "id": c.id,
                            "email": build_runtime_email(c.inbound_tag, c.email),
                            "flow": c.flow if (c.flow and flow_allowed) else "",
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
                    used_wg_ips = set()
                    for c in ib.clients:
                        if c.enable:
                            pub_key = _derive_wg_pubkey(c.id)
                            if pub_key:
                                allowed_ip = _wg_peer_ip(c.id, used_wg_ips)
                                if allowed_ip:
                                    peers.append(
                                        {
                                            "publicKey": pub_key,
                                            "allowedIPs": [allowed_ip],
                                        }
                                    )
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
                    if ib.port == 443:
                        sockopt = dict(stream_settings_for_xray.get("sockopt") or {})
                        sockopt["acceptProxyProtocol"] = True
                        sockopt.setdefault("tcpKeepAliveInterval", -1)
                        sockopt.setdefault("tcpFastOpen", True)
                        stream_settings_for_xray["sockopt"] = sockopt
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

            candidate = CANDIDATE_PATH
            with open(candidate, "w", encoding="utf-8") as f:
                json.dump(full_config, f, indent=2)
            try:
                if validate:
                    _validate_xray_config(candidate)
                os.rename(candidate, CONFIG_PATH)
            finally:
                if os.path.exists(candidate):
                    try:
                        os.remove(candidate)
                    except OSError:
                        pass
        logger.info(
            "config regenerated in %.0f ms (%d inbounds, %d outbounds, validate=%s)",
            (time.monotonic() - t0) * 1000,
            len(inbounds_json) - 1,
            len(outbounds_json),
            validate,
        )
    except Timeout:
        raise Exception("Could not acquire lock")
