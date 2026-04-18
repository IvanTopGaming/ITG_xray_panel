import base64
import binascii
import html
import json
import os
import time
from urllib.parse import quote, urlencode
import yaml
from flask import Blueprint, request, Response
from app.extensions import limiter, db
from app.models import Client, Inbound, NodeClientTraffic, SystemSetting
from app.services import sub_cache


def _master_groups():
    """Return master panel's tag set (read from SystemSetting)."""
    setting = SystemSetting.query.filter_by(key="master_groups").first()
    raw = (setting.value if setting else "") or ""
    return {g.strip() for g in raw.split(",") if g.strip()}


def _master_visible_to_client(client):
    """Whether the master inbound should be exposed in this user's subscription.

    Same rule as remote nodes:
    - user with no allowed_node_groups → no filter, master always visible
    - user with allowed_node_groups + master has no tags → master is "common", visible
    - user with allowed_node_groups + master has tags → must overlap
    """
    if client is None:
        return True
    allowed = {g.strip() for g in (client.allowed_node_groups or "").split(",") if g.strip()}
    if not allowed:
        return True
    master = _master_groups()
    if not master:
        return True
    return bool(master & allowed)


bp = Blueprint("subscription", __name__)


def _get_remote_links(client_email, panel_client=None):
    """Fetch subscription links from all active remote nodes for a given user email."""
    try:
        from app.services.node_sync import get_aggregated_sub_links

        return get_aggregated_sub_links(client_email, client=panel_client)
    except Exception:
        return []


def _get_remote_clash_proxies(client_email, panel_client=None):
    """Fetch Clash proxy nodes from all active remote nodes."""
    try:
        from app.services.node_sync import get_remote_configs

        configs = get_remote_configs(client_email, "clash", client=panel_client)
        proxies = []
        for node_name, raw_config in configs:
            try:
                config = yaml.safe_load(raw_config)
                for proxy in config.get("proxies", []):
                    proxy["name"] = f"{node_name}-{proxy.get('name', 'proxy')}"
                    proxies.append(proxy)
            except Exception:
                pass
        return proxies
    except Exception:
        return []


def _get_remote_singbox_outbounds(client_email, panel_client=None):
    """Fetch sing-box outbound entries from all active remote nodes."""
    try:
        from app.services.node_sync import get_remote_configs

        configs = get_remote_configs(client_email, "singbox", client=panel_client)
        outbounds = []
        for node_name, raw_config in configs:
            try:
                config = json.loads(raw_config)
                for ob in config.get("outbounds", []):
                    if ob.get("type") in ("direct", "block", "dns"):
                        continue
                    ob["tag"] = f"{node_name}-{ob.get('tag', 'proxy')}"
                    outbounds.append(ob)
            except Exception:
                pass
        return outbounds
    except Exception:
        return []


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


_KNOWN_CLIENT_UA_TOKENS = (
    "clash",
    "meta",
    "stash",
    "sing-box",
    "nekobox",
    "v2ray",
    "v2rayng",
    "v2box",
    "shadowrocket",
    "quantumult",
    "loon",
    "surge",
    "hiddify",
    "streisand",
    "fair",
    "happ",
)


def _looks_like_browser(user_agent: str) -> bool:
    if not user_agent:
        return False
    ua = user_agent.lower()
    if any(token in ua for token in _KNOWN_CLIENT_UA_TOKENS):
        return False
    # Common browser identifiers — covers Chrome, Firefox, Safari, Edge, Opera.
    return any(token in ua for token in ("mozilla", "applewebkit", "gecko", "trident", "edg"))


@bp.route("/sub/<path:uuid_str>", methods=["GET"])
@limiter.limit("180 per minute")
def get_subscription(uuid_str):
    user_agent = request.headers.get("User-Agent", "").lower()

    # Allow the in-browser landing page to force a specific format via ?ua=…
    forced_ua = (request.args.get("ua", "") or "").strip().lower()
    if forced_ua in ("clash", "meta", "stash"):
        user_agent = "clash"
    elif forced_ua in ("singbox", "sing-box", "nekobox"):
        user_agent = "sing-box"
    elif forced_ua in ("v2ray", "v2rayng", "raw"):
        user_agent = "v2ray"

    if _looks_like_browser(user_agent):
        html = render_subscription_page(uuid_str)
        if html is None:
            return "User not found", 404
        return Response(html, mimetype="text/html; charset=utf-8")

    if any(x in user_agent for x in ["clash", "meta", "stash"]):
        cached = sub_cache.get("clash", uuid_str)
        if cached is not None:
            return Response(
                cached,
                mimetype="text/yaml",
                headers={
                    "Content-Disposition": 'attachment; filename="config.yaml"',
                    "Profile-Update-Interval": "24",
                },
            )
        config = generate_clash_config(uuid_str)
        if not config:
            return "User not found", 404
        sub_cache.set("clash", uuid_str, config)
        return Response(
            config,
            mimetype="text/yaml",
            headers={
                "Content-Disposition": 'attachment; filename="config.yaml"',
                "Profile-Update-Interval": "24",
            },
        )

    if any(x in user_agent for x in ["sing-box", "nekobox"]):
        cached = sub_cache.get("singbox", uuid_str)
        if cached is not None:
            return Response(
                cached,
                mimetype="application/json",
                headers={
                    "Content-Disposition": 'attachment; filename="config.json"',
                    "Profile-Update-Interval": "24",
                },
            )
        config = generate_singbox_config(uuid_str)
        if not config:
            return "User not found", 404
        sub_cache.set("singbox", uuid_str, config)
        return Response(
            config,
            mimetype="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="config.json"',
                "Profile-Update-Interval": "24",
            },
        )

    cached = sub_cache.get("v2ray", uuid_str)
    if cached is not None:
        return Response(
            cached,
            mimetype="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="config.txt"',
                "Profile-Update-Interval": "24",
            },
        )
    links = get_subscription_content(uuid_str)
    if not links:
        return "User not found", 404
    text_content = "\n".join(links)
    encoded = base64.b64encode(text_content.encode("utf-8")).decode("utf-8")
    sub_cache.set("v2ray", uuid_str, encoded)
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
        local = [link] if _master_visible_to_client(client) else []
        return local + _get_remote_links(client.email, panel_client=client)

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
        local = (
            [f"vmess://{base64.b64encode(json.dumps(v_conf).encode()).decode()}"]
            if _master_visible_to_client(client)
            else []
        )
        return local + _get_remote_links(client.email, panel_client=client)

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
        local = (
            [
                f"trojan://{quote(str(client.id), safe='')}@{host}:{ib.port}?{urlencode(query)}#{quote(client.email, safe='')}"
            ]
            if _master_visible_to_client(client)
            else []
        )
        return local + _get_remote_links(client.email, panel_client=client)

    elif ib.protocol == "shadowsocks":
        method = stream.get("ssMethod", "2022-blake3-aes-128-gcm")
        server_pass = str(stream.get("ssPassword", "") or "").strip()
        user_pass = str(client.id or "").strip()
        if _is_ss2022_method(method):
            server_pass = _normalize_ss2022_key(server_pass)
            user_pass = _normalize_ss2022_key(user_pass)
        user_part = f"{method}:{server_pass}:{user_pass}" if _is_ss2022_method(method) else f"{method}:{user_pass}"
        b64_user = base64.b64encode(user_part.encode()).decode()
        local_links = (
            [f"ss://{b64_user}@{host}:{ib.port}#{quote(client.email, safe='')}"]
            if _master_visible_to_client(client)
            else []
        )
        return local_links + _get_remote_links(client.email, panel_client=client)

    return _get_remote_links(client.email, panel_client=client) or []


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

    if _master_visible_to_client(client):
        all_proxies = [proxy_node]
        all_proxy_names = [proxy_node["name"]]
    else:
        all_proxies = []
        all_proxy_names = []

    remote_proxies = _get_remote_clash_proxies(client.email, panel_client=client)
    for rp in remote_proxies:
        all_proxies.append(rp)
        all_proxy_names.append(rp["name"])

    config = {
        "port": 7890,
        "socks-port": 7891,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "proxies": all_proxies,
        "proxy-groups": [
            {
                "name": "FASTEST",
                "type": "url-test",
                "url": "http://www.gstatic.com/generate_204",
                "interval": 300,
                "proxies": all_proxy_names,
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

    all_outbounds = [outbound] if _master_visible_to_client(client) else []

    remote_outbounds = _get_remote_singbox_outbounds(client.email, panel_client=client)
    all_outbounds.extend(remote_outbounds)

    config = {
        "log": {"level": "info", "timestamp": True},
        "dns": {
            "servers": [
                {"tag": "google", "address": "8.8.8.8", "detour": "proxy"},
                {"tag": "local", "address": "local", "detour": "direct"},
            ]
        },
        "inbounds": [{"type": "tun", "inet4_address": "172.19.0.1/30", "auto_route": True}],
        "outbounds": all_outbounds + [{"type": "direct", "tag": "direct"}],
        "route": {"auto_detect_interface": True},
    }
    return json.dumps(config, indent=2)


def _format_bytes(n: int) -> str:
    n = int(n or 0)
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.2f} {units[i]}"


def _format_expiry(ms: int) -> str:
    if not ms or ms <= 0:
        return "Never"
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ms / 1000))
    except (OSError, OverflowError, ValueError):
        return "Unknown"


def render_subscription_page(uuid_str: str):
    """Render an HTML landing page for browser visitors hitting /api/sub/<uuid>."""
    client = db.session.get(Client, uuid_str)
    if not client:
        return None
    ib = Inbound.query.filter_by(tag=client.inbound_tag).first()
    if not ib:
        return None

    # Resolve aggregated usage from polled per-node counters.
    node_rows = NodeClientTraffic.query.filter_by(email=client.email).all()
    nodes_total = sum(int(r.up or 0) + int(r.down or 0) for r in node_rows)
    master_total = int(client.up or 0) + int(client.down or 0)
    aggregate_total = master_total + nodes_total

    per_node_limit = int(client.limit_bytes or 0)
    global_limit = int(client.global_limit_bytes or 0)

    # Build the absolute subscription URL with the secret-path prefix from PANEL_BASE_URL.
    panel_base = os.getenv("PANEL_BASE_URL", "").rstrip("/")
    sub_path = f"/api/sub/{quote(uuid_str, safe='')}"
    sub_url = f"{panel_base}{sub_path}" if panel_base else sub_path
    abs_sub_url = sub_url
    if not abs_sub_url.startswith("http"):
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme or "https")
        host = request.headers.get("X-Forwarded-Host", request.host)
        abs_sub_url = f"{scheme}://{host}{sub_url}"

    # Build a friendly node list (master + remote nodes).
    try:
        from app.models import Node

        node_objs = Node.query.filter_by(enable=True).all()
    except Exception:
        node_objs = []
    allowed = {g.strip() for g in (client.allowed_node_groups or "").split(",") if g.strip()}
    master_groups_set = _master_groups()
    node_items = []
    if _master_visible_to_client(client):
        node_items.append(
            {
                "name": "Master",
                "groups": sorted(master_groups_set),
                "status": "online",
            }
        )
    for n in node_objs:
        node_groups = {g.strip() for g in (n.groups or "").split(",") if g.strip()}
        if allowed and node_groups and not (allowed & node_groups):
            continue
        node_items.append(
            {
                "name": n.name,
                "groups": sorted(node_groups),
                "status": n.status or "unknown",
            }
        )

    def _esc(s):
        return html.escape(str(s or ""))

    def _bar(used, limit):
        if limit <= 0:
            return ""
        pct = min(100.0, used * 100.0 / limit)
        color = "#7c4dff"
        if pct >= 90:
            color = "#ef4444"
        elif pct >= 75:
            color = "#f59e0b"
        return (
            '<div class="bar"><div class="bar-fill" style="width:'
            f'{pct:.1f}%;background:{color}"></div></div>'
            f'<div class="bar-meta">{_esc(_format_bytes(used))} / {_esc(_format_bytes(limit))} '
            f"({pct:.1f}%)</div>"
        )

    nodes_html_parts = []
    for n in node_items:
        groups_html = " ".join(f'<span class="tag">{_esc(g)}</span>' for g in n["groups"])
        status_class = "ok" if n["status"] == "online" else ("err" if n["status"] == "offline" else "unk")
        nodes_html_parts.append(
            f'<li><span class="dot {status_class}"></span>'
            f'<span class="node-name">{_esc(n["name"])}</span>{groups_html}</li>'
        )
    nodes_html = "".join(nodes_html_parts) or "<li>No nodes</li>"

    enabled_badge = '<span class="pill ok">Active</span>' if client.enable else '<span class="pill err">Disabled</span>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Subscription · {_esc(client.email)}</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #0b0b13;
    --card: rgba(255,255,255,0.04);
    --border: rgba(255,255,255,0.08);
    --text: #e6e6f0;
    --muted: #9090a8;
    --primary: #b39bff;
    --accent: #7c4dff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px 16px;
    font-family: -apple-system, Segoe UI, Roboto, Inter, sans-serif;
    background: radial-gradient(1200px 600px at 50% -10%, rgba(124,77,255,0.18), transparent 60%), var(--bg);
    color: var(--text);
    min-height: 100vh;
  }}
  .wrap {{ max-width: 720px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); font-size: 14px; margin-bottom: 24px; }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 18px;
    padding: 20px;
    margin-bottom: 16px;
    backdrop-filter: blur(8px);
  }}
  .row {{ display: flex; justify-content: space-between; gap: 12px; padding: 8px 0; }}
  .row + .row {{ border-top: 1px solid rgba(255,255,255,0.05); }}
  .label {{ color: var(--muted); font-size: 13px; }}
  .val {{ font-size: 14px; font-weight: 500; }}
  .pill {{
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 12px; font-weight: 600; letter-spacing: .2px;
  }}
  .pill.ok {{ background: rgba(34,197,94,0.15); color: #4ade80; }}
  .pill.err {{ background: rgba(239,68,68,0.15); color: #f87171; }}
  .bar {{ height: 8px; background: rgba(255,255,255,0.06); border-radius: 999px; overflow: hidden; margin-top: 6px; }}
  .bar-fill {{ height: 100%; transition: width .4s ease; }}
  .bar-meta {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  .url-box {{
    display: flex; gap: 8px; align-items: center;
    background: rgba(0,0,0,0.3); border: 1px solid var(--border);
    border-radius: 12px; padding: 10px 12px; font-family: ui-monospace, Menlo, monospace;
    font-size: 12px; word-break: break-all;
  }}
  button, .btn {{
    cursor: pointer; border: none; border-radius: 10px;
    padding: 10px 14px; font-weight: 600; font-size: 13px;
    background: linear-gradient(135deg, var(--primary), var(--accent));
    color: #fff; text-decoration: none; display: inline-block;
  }}
  button.secondary {{
    background: rgba(255,255,255,0.06); color: var(--text);
    border: 1px solid var(--border);
  }}
  .actions {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }}
  ul.nodes {{ list-style: none; padding: 0; margin: 0; }}
  ul.nodes li {{
    display: flex; align-items: center; gap: 8px;
    padding: 8px 0; border-top: 1px solid rgba(255,255,255,0.05);
  }}
  ul.nodes li:first-child {{ border-top: none; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; background: #555; }}
  .dot.ok {{ background: #4ade80; box-shadow: 0 0 8px rgba(74,222,128,0.6); }}
  .dot.err {{ background: #f87171; }}
  .dot.unk {{ background: #888; }}
  .node-name {{ flex: 1; font-weight: 500; }}
  .tag {{
    background: rgba(124,77,255,0.18); color: var(--primary);
    padding: 2px 8px; border-radius: 999px; font-size: 11px;
  }}
  h2 {{ font-size: 14px; color: var(--muted); margin: 0 0 12px; text-transform: uppercase; letter-spacing: .8px; }}
  .toast {{
    position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
    background: #1a1a26; border: 1px solid var(--border); padding: 10px 16px;
    border-radius: 999px; font-size: 13px; opacity: 0; transition: opacity .25s;
  }}
  .toast.show {{ opacity: 1; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>{_esc(client.email)}</h1>
    <div class="sub">Subscription overview · {enabled_badge}</div>

    <div class="card">
      <h2>Subscription URL</h2>
      <div class="url-box" id="suburl">{_esc(abs_sub_url)}</div>
      <div class="actions">
        <button onclick="copySub()">Copy URL</button>
        <a class="btn secondary" href="{_esc(sub_url)}?ua=v2ray" download="config.txt">Download v2ray</a>
        <a class="btn secondary" href="{_esc(sub_url)}?ua=clash" download="config.yaml">Download Clash</a>
        <a class="btn secondary" href="{_esc(sub_url)}?ua=singbox" download="config.json">Download sing-box</a>
      </div>
    </div>

    <div class="card">
      <h2>Usage</h2>
      <div class="row"><span class="label">Master node</span><span class="val">{_esc(_format_bytes(master_total))}</span></div>
      <div class="row"><span class="label">All remote nodes</span><span class="val">{_esc(_format_bytes(nodes_total))}</span></div>
      <div class="row"><span class="label">Aggregate (master + nodes)</span><span class="val">{_esc(_format_bytes(aggregate_total))}</span></div>
      {('<div class="row"><span class="label">Per-node limit</span><span class="val">' + _esc(_format_bytes(per_node_limit)) + "</span></div>" + _bar(master_total, per_node_limit)) if per_node_limit > 0 else ""}
      {('<div class="row"><span class="label">Global limit</span><span class="val">' + _esc(_format_bytes(global_limit)) + "</span></div>" + _bar(aggregate_total, global_limit)) if global_limit > 0 else ""}
      <div class="row"><span class="label">Expires</span><span class="val">{_esc(_format_expiry(client.expiry_time))}</span></div>
    </div>

    <div class="card">
      <h2>Servers</h2>
      <ul class="nodes">{nodes_html}</ul>
    </div>
  </div>
  <div class="toast" id="toast">Copied to clipboard</div>
<script>
function showToast(msg) {{
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(function() {{ t.classList.remove('show'); }}, 1600);
}}
function copySub() {{
  var url = document.getElementById('suburl').textContent.trim();
  if (navigator.clipboard) {{
    navigator.clipboard.writeText(url).then(function() {{ showToast('Copied to clipboard'); }});
  }} else {{
    var ta = document.createElement('textarea');
    ta.value = url; document.body.appendChild(ta); ta.select();
    try {{ document.execCommand('copy'); showToast('Copied to clipboard'); }} catch (e) {{}}
    document.body.removeChild(ta);
  }}
}}
</script>
</body>
</html>"""
