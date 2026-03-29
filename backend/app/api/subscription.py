import base64
import binascii
import json
import os
from urllib.parse import quote, urlencode
import yaml
from flask import Blueprint, request, Response
from app.extensions import limiter, db
from app.models import Client, Inbound

bp = Blueprint("subscription", __name__)
SS2022_METHODS = {
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
}


def _normalize_reality_public_key(public_key):
    key = (public_key or "").strip()
    if not key:
        return ""

    # Accept both std/base64 and urlsafe/base64 keys, output canonical urlsafe form.
    padded = key + ("=" * ((4 - len(key) % 4) % 4))
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            raw = decoder(padded)
            if len(raw) == 32:
                return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
        except (ValueError, binascii.Error):
            continue
    return key.replace("+", "-").replace("/", "_").rstrip("=")


def _is_ss2022_method(method):
    return str(method or "").strip().lower() in SS2022_METHODS


def _normalize_ss2022_key(value):
    key = str(value or "").strip()
    if not key:
        return ""
    padded = key + ("=" * ((4 - len(key) % 4) % 4))
    for decoder in (
        lambda raw: base64.b64decode(raw, validate=True),
        lambda raw: base64.b64decode(raw, altchars=b"-_", validate=True),
    ):
        try:
            decoded = decoder(padded)
        except (ValueError, binascii.Error):
            continue
        if decoded:
            return base64.b64encode(decoded).decode("utf-8")
    return key


def _extract_tls_server_name(stream):
    tls_settings = stream.get("tlsSettings", {})
    if isinstance(tls_settings, dict):
        name = str(tls_settings.get("serverName", "") or "").strip()
        if name:
            return name

    ws_headers = stream.get("wsSettings", {}).get("headers", {})
    if isinstance(ws_headers, dict):
        return str(ws_headers.get("Host", "") or "").strip()
    return ""


def _extract_tls_alpn(stream):
    tls_settings = stream.get("tlsSettings", {})
    if not isinstance(tls_settings, dict):
        return []

    raw = tls_settings.get("alpn", [])
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def _extract_tls_utls_fingerprint(stream):
    tls_settings = stream.get("tlsSettings", {})
    if not isinstance(tls_settings, dict):
        return ""
    return str(tls_settings.get("_utlsFingerprint", "") or "").strip()


def _extract_transport_path_host(stream):
    network = stream.get("network", "tcp")
    path = ""
    host = ""

    if network == "ws":
        ws_settings = stream.get("wsSettings", {})
        path = str(ws_settings.get("path", "/") or "/")
        headers = ws_settings.get("headers", {})
        if isinstance(headers, dict):
            host = str(headers.get("Host", "") or "").strip()
    elif network == "xhttp":
        xhttp_settings = stream.get("xhttpSettings", {})
        if isinstance(xhttp_settings, dict):
            path = str(xhttp_settings.get("path", "/") or "/")
            host = str(xhttp_settings.get("host", "") or "").strip()
    elif network == "httpupgrade":
        http_upgrade_settings = stream.get("httpUpgradeSettings", {})
        if isinstance(http_upgrade_settings, dict):
            path = str(http_upgrade_settings.get("path", "/") or "/")
            host = str(http_upgrade_settings.get("host", "") or "").strip()
    elif network == "splithttp":
        split_http_settings = stream.get("splitHttpSettings", {})
        if isinstance(split_http_settings, dict):
            path = str(split_http_settings.get("path", "/") or "/")
            host = str(split_http_settings.get("host", "") or "").strip()

    return path, host


def _apply_clash_transport(proxy_node, stream):
    network = str(stream.get("network", "tcp") or "tcp").strip().lower()
    path, host = _extract_transport_path_host(stream)

    if network == "grpc":
        proxy_node["network"] = "grpc"
        proxy_node["grpc-opts"] = {"grpc-service-name": stream.get("grpcSettings", {}).get("serviceName", "grpc")}
        return

    if network == "ws":
        proxy_node["network"] = "ws"
        proxy_node["ws-opts"] = {"path": path or "/"}
        if host:
            proxy_node["ws-opts"]["headers"] = {"Host": host}
        return

    if network == "httpupgrade":
        proxy_node["network"] = "ws"
        proxy_node["ws-opts"] = {
            "path": path or "/",
            "v2ray-http-upgrade": True,
        }
        if host:
            proxy_node["ws-opts"]["headers"] = {"Host": host}
        return

    if network in ["xhttp", "splithttp"]:
        proxy_node["network"] = "http"
        proxy_node["http-opts"] = {"path": [path or "/"]}
        if host:
            proxy_node["http-opts"]["headers"] = {"Host": [host]}
        return

    proxy_node["network"] = network


def _apply_singbox_transport(outbound, stream):
    network = str(stream.get("network", "tcp") or "tcp").strip().lower()
    path, host = _extract_transport_path_host(stream)

    if network == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": stream.get("grpcSettings", {}).get("serviceName", "grpc"),
        }
        return

    if network == "ws":
        outbound["transport"] = {
            "type": "ws",
            "path": path or "/",
        }
        if host:
            outbound["transport"]["headers"] = {"Host": host}
        return

    if network == "httpupgrade":
        outbound["transport"] = {
            "type": "httpupgrade",
            "path": path or "/",
        }
        if host:
            outbound["transport"]["host"] = host
        return

    if network in ["xhttp", "splithttp"]:
        outbound["transport"] = {
            "type": "http",
            "path": path or "/",
        }
        if host:
            outbound["transport"]["host"] = [host]
        return


@bp.route("/sub/<path:uuid_str>", methods=["GET"])
@limiter.limit("180 per minute")
def get_subscription(uuid_str):
    user_agent = request.headers.get("User-Agent", "").lower()

    if any(x in user_agent for x in ["clash", "meta", "stash"]):
        config = generate_clash_config(uuid_str)
        if not config:
            return "User not found", 404
        return Response(
            config,
            mimetype="text/yaml",
            headers={
                "Content-Disposition": 'attachment; filename="config.yaml"',
                "Profile-Update-Interval": "24",
            },
        )

    if any(x in user_agent for x in ["sing-box", "nekobox"]):
        config = generate_singbox_config(uuid_str)
        if not config:
            return "User not found", 404
        return Response(
            config,
            mimetype="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="config.json"',
                "Profile-Update-Interval": "24",
            },
        )

    links = get_subscription_content(uuid_str)
    if not links:
        return "User not found", 404
    text_content = "\n".join(links)
    encoded = base64.b64encode(text_content.encode("utf-8")).decode("utf-8")
    return Response(
        encoded,
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="config.txt"',
            "Profile-Update-Interval": "24",
        },
    )


def get_subscription_content(uuid_str):
    client = db.session.get(Client, uuid_str)
    if not client or not client.enable:
        return None
    ib = Inbound.query.filter_by(tag=client.inbound_tag).first()
    if not ib:
        return None
    stream = json.loads(ib.stream_settings)
    host = os.getenv("PANEL_DOMAIN", "localhost")

    if ib.protocol == "vless":
        network = stream.get("network", "tcp")
        security = stream.get("security", "none")
        r_sets = stream.get("realitySettings", {})
        flow = client.flow or ""
        query = {
            "type": network,
            "security": security,
        }
        if network == "grpc":
            query["serviceName"] = stream.get("grpcSettings", {}).get("serviceName", "grpc")
        else:
            path, transport_host = _extract_transport_path_host(stream)
            if path:
                query["path"] = path
            if transport_host:
                query["host"] = transport_host

        if security == "reality":
            sid = r_sets.get("shortIds", [""])[0]
            pbk = _normalize_reality_public_key(r_sets.get("publicKey", ""))
            sni = r_sets.get("serverNames", ["google.com"])[0]
            fp = r_sets.get("fingerprint", "chrome")
            spx = r_sets.get("spiderX", "")

            query["pbk"] = pbk
            query["fp"] = fp
            query["sni"] = sni
            query["sid"] = sid
            if spx:
                query["spx"] = spx
        elif security == "tls":
            tls_sni = _extract_tls_server_name(stream)
            if tls_sni:
                query["sni"] = tls_sni
            tls_alpn = _extract_tls_alpn(stream)
            if tls_alpn:
                query["alpn"] = ",".join(tls_alpn)
            tls_fp = _extract_tls_utls_fingerprint(stream)
            if tls_fp:
                query["fp"] = tls_fp

        if flow:
            query["flow"] = flow
        link = (
            f"vless://{quote(str(client.id), safe='')}@{host}:{ib.port}?"
            f"{urlencode(query)}#{quote(client.email, safe='')}"
        )
        return [link]

    elif ib.protocol == "vmess":
        v_conf = {
            "v": "2",
            "ps": client.email,
            "add": host,
            "port": ib.port,
            "id": client.id,
            "aid": "0",
            "net": stream["network"],
            "type": "none",
            "host": "",
            "path": "",
            "tls": stream["security"],
        }
        if stream["network"] == "ws":
            v_conf["path"] = stream.get("wsSettings", {}).get("path", "/")
            v_conf["host"] = stream.get("wsSettings", {}).get("headers", {}).get("Host", "")
        if stream.get("security") == "tls":
            tls_sni = _extract_tls_server_name(stream)
            if tls_sni:
                v_conf["sni"] = tls_sni
        return [f"vmess://{base64.b64encode(json.dumps(v_conf).encode()).decode()}"]

    elif ib.protocol == "trojan":
        security = stream.get("security", "none")
        network = stream.get("network", "tcp")
        query = {
            "security": security,
            "type": network,
        }
        if network == "grpc":
            query["serviceName"] = stream.get("grpcSettings", {}).get("serviceName", "grpc")
        else:
            path, transport_host = _extract_transport_path_host(stream)
            if path:
                query["path"] = path
            if transport_host:
                query["host"] = transport_host
        if security == "reality":
            r_sets = stream.get("realitySettings", {})
            query["pbk"] = _normalize_reality_public_key(r_sets.get("publicKey", ""))
            query["fp"] = r_sets.get("fingerprint", "chrome")
            query["sni"] = r_sets.get("serverNames", ["google.com"])[0]
            query["sid"] = r_sets.get("shortIds", [""])[0]
            spx = r_sets.get("spiderX", "")
            if spx:
                query["spx"] = spx
        elif security == "tls":
            tls_sni = _extract_tls_server_name(stream)
            if tls_sni:
                query["sni"] = tls_sni
            tls_alpn = _extract_tls_alpn(stream)
            if tls_alpn:
                query["alpn"] = ",".join(tls_alpn)
            tls_fp = _extract_tls_utls_fingerprint(stream)
            if tls_fp:
                query["fp"] = tls_fp
        return [
            f"trojan://{quote(str(client.id), safe='')}@{host}:{ib.port}?{urlencode(query)}#{quote(client.email, safe='')}"
        ]

    elif ib.protocol == "shadowsocks":
        method = stream.get("ssMethod", "2022-blake3-aes-128-gcm")
        server_pass = str(stream.get("ssPassword", "") or "").strip()
        user_pass = str(client.id or "").strip()
        if _is_ss2022_method(method):
            server_pass = _normalize_ss2022_key(server_pass)
            user_pass = _normalize_ss2022_key(user_pass)
        user_part = f"{method}:{server_pass}:{user_pass}" if _is_ss2022_method(method) else f"{method}:{user_pass}"
        b64_user = base64.b64encode(user_part.encode()).decode()
        return [f"ss://{b64_user}@{host}:{ib.port}#{quote(client.email, safe='')}"]

    return []


def generate_clash_config(uuid_str):
    client = db.session.get(Client, uuid_str)
    if not client or not client.enable:
        return None
    ib = Inbound.query.filter_by(tag=client.inbound_tag).first()
    if not ib:
        return None
    stream = json.loads(ib.stream_settings)
    host = os.getenv("PANEL_DOMAIN", "localhost")
    proxy_node = {
        "name": f"{ib.tag}-{client.email}",
        "server": host,
        "port": ib.port,
        "type": ib.protocol if ib.protocol != "shadowsocks" else "ss",
    }

    if ib.protocol == "vless":
        security = stream.get("security", "none")
        proxy_node.update(
            {
                "uuid": client.id,
                "network": stream["network"],
                "udp": True,
            }
        )
        if security in ["tls", "reality"]:
            proxy_node["tls"] = True

        if security == "reality":
            r_sets = stream.get("realitySettings", {})
            proxy_node["servername"] = r_sets.get("serverNames", ["google.com"])[0]
            proxy_node["client-fingerprint"] = r_sets.get("fingerprint", "chrome")
            proxy_node["reality-opts"] = {
                "public-key": _normalize_reality_public_key(r_sets.get("publicKey", "")),
                "short-id": r_sets.get("shortIds", [""])[0],
            }
        elif security == "tls":
            tls_sni = _extract_tls_server_name(stream)
            if tls_sni:
                proxy_node["servername"] = tls_sni
            tls_fp = _extract_tls_utls_fingerprint(stream)
            if tls_fp:
                proxy_node["client-fingerprint"] = tls_fp

        if client.flow:
            proxy_node["flow"] = client.flow

    elif ib.protocol == "vmess":
        proxy_node.update(
            {
                "uuid": client.id,
                "cipher": "auto",
                "network": stream["network"],
                "tls": stream["security"] == "tls",
                "udp": True,
            }
        )
        if stream.get("security") == "tls":
            tls_sni = _extract_tls_server_name(stream)
            if tls_sni:
                proxy_node["servername"] = tls_sni
            tls_fp = _extract_tls_utls_fingerprint(stream)
            if tls_fp:
                proxy_node["client-fingerprint"] = tls_fp

    elif ib.protocol == "trojan":
        security = stream.get("security", "none")
        proxy_node.update({"password": client.id, "network": stream["network"], "udp": True})
        if security in ["tls", "reality"]:
            proxy_node["tls"] = True
        if security == "reality":
            r_sets = stream.get("realitySettings", {})
            proxy_node["servername"] = r_sets.get("serverNames", ["google.com"])[0]
            proxy_node["client-fingerprint"] = r_sets.get("fingerprint", "chrome")
            proxy_node["reality-opts"] = {
                "public-key": _normalize_reality_public_key(r_sets.get("publicKey", "")),
                "short-id": r_sets.get("shortIds", [""])[0],
            }
        elif security == "tls":
            tls_sni = _extract_tls_server_name(stream)
            if tls_sni:
                proxy_node["servername"] = tls_sni
            tls_fp = _extract_tls_utls_fingerprint(stream)
            if tls_fp:
                proxy_node["client-fingerprint"] = tls_fp

    elif ib.protocol == "shadowsocks":
        method = stream.get("ssMethod", "chacha20-poly1305")
        server_pass = str(stream.get("ssPassword", "") or "").strip()
        user_pass = str(client.id or "").strip()
        if _is_ss2022_method(method):
            server_pass = _normalize_ss2022_key(server_pass)
            user_pass = _normalize_ss2022_key(user_pass)
        proxy_node["cipher"] = method
        proxy_node["password"] = f"{server_pass}:{user_pass}" if _is_ss2022_method(method) else user_pass

    _apply_clash_transport(proxy_node, stream)

    config = {
        "port": 7890,
        "socks-port": 7891,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "proxies": [proxy_node],
        "proxy-groups": [
            {
                "name": "FASTEST",
                "type": "url-test",
                "url": "http://www.gstatic.com/generate_204",
                "interval": 300,
                "proxies": [proxy_node["name"]],
            }
        ],
        "rules": ["GEOIP,CN,DIRECT", "MATCH,FASTEST"],
    }
    return yaml.dump(config, sort_keys=False, allow_unicode=True)


def generate_singbox_config(uuid_str):
    client = db.session.get(Client, uuid_str)
    if not client or not client.enable:
        return None
    ib = Inbound.query.filter_by(tag=client.inbound_tag).first()
    if not ib:
        return None
    stream = json.loads(ib.stream_settings)
    host = os.getenv("PANEL_DOMAIN", "localhost")
    outbound = {
        "tag": "proxy",
        "server": host,
        "server_port": ib.port,
        "type": ib.protocol,
    }

    if ib.protocol == "vless":
        security = stream.get("security", "none")
        outbound.update({"uuid": client.id, "packet_encoding": "xudp"})
        if client.flow:
            outbound["flow"] = client.flow
        if security == "reality":
            r_sets = stream.get("realitySettings", {})
            outbound["tls"] = {
                "enabled": True,
                "server_name": r_sets.get("serverNames", ["google.com"])[0],
                "utls": {
                    "enabled": True,
                    "fingerprint": r_sets.get("fingerprint", "chrome"),
                },
                "reality": {
                    "enabled": True,
                    "public_key": _normalize_reality_public_key(r_sets.get("publicKey", "")),
                    "short_id": r_sets.get("shortIds", [""])[0],
                },
            }
        elif security == "tls":
            tls_payload = {"enabled": True}
            tls_sni = _extract_tls_server_name(stream)
            if tls_sni:
                tls_payload["server_name"] = tls_sni
            tls_alpn = _extract_tls_alpn(stream)
            if tls_alpn:
                tls_payload["alpn"] = tls_alpn
            tls_fp = _extract_tls_utls_fingerprint(stream)
            if tls_fp:
                tls_payload["utls"] = {"enabled": True, "fingerprint": tls_fp}
            outbound["tls"] = tls_payload
    elif ib.protocol == "vmess":
        outbound.update({"uuid": client.id, "security": "auto"})
        if stream["security"] == "tls":
            tls_payload = {"enabled": True}
            tls_sni = _extract_tls_server_name(stream)
            if tls_sni:
                tls_payload["server_name"] = tls_sni
            tls_alpn = _extract_tls_alpn(stream)
            if tls_alpn:
                tls_payload["alpn"] = tls_alpn
            tls_fp = _extract_tls_utls_fingerprint(stream)
            if tls_fp:
                tls_payload["utls"] = {"enabled": True, "fingerprint": tls_fp}
            outbound["tls"] = tls_payload
    elif ib.protocol == "trojan":
        security = stream.get("security", "none")
        outbound["password"] = client.id
        if security == "reality":
            r_sets = stream.get("realitySettings", {})
            outbound["tls"] = {
                "enabled": True,
                "server_name": r_sets.get("serverNames", ["google.com"])[0],
                "utls": {
                    "enabled": True,
                    "fingerprint": r_sets.get("fingerprint", "chrome"),
                },
                "reality": {
                    "enabled": True,
                    "public_key": _normalize_reality_public_key(r_sets.get("publicKey", "")),
                    "short_id": r_sets.get("shortIds", [""])[0],
                },
            }
        elif security == "tls":
            tls_payload = {"enabled": True}
            tls_sni = _extract_tls_server_name(stream)
            if tls_sni:
                tls_payload["server_name"] = tls_sni
            tls_alpn = _extract_tls_alpn(stream)
            if tls_alpn:
                tls_payload["alpn"] = tls_alpn
            tls_fp = _extract_tls_utls_fingerprint(stream)
            if tls_fp:
                tls_payload["utls"] = {"enabled": True, "fingerprint": tls_fp}
            outbound["tls"] = tls_payload
    elif ib.protocol == "shadowsocks":
        method = stream.get("ssMethod", "chacha20-poly1305")
        outbound["method"] = method
        server_pass = str(stream.get("ssPassword", "") or "").strip()
        user_pass = str(client.id or "").strip()
        if _is_ss2022_method(method):
            server_pass = _normalize_ss2022_key(server_pass)
            user_pass = _normalize_ss2022_key(user_pass)
        outbound["password"] = f"{server_pass}:{user_pass}" if _is_ss2022_method(method) else user_pass

    _apply_singbox_transport(outbound, stream)

    config = {
        "log": {"level": "info", "timestamp": True},
        "dns": {
            "servers": [
                {"tag": "google", "address": "8.8.8.8", "detour": "proxy"},
                {"tag": "local", "address": "local", "detour": "direct"},
            ]
        },
        "inbounds": [{"type": "tun", "inet4_address": "172.19.0.1/30", "auto_route": True}],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        "route": {"auto_detect_interface": True},
    }
    return json.dumps(config, indent=2)
