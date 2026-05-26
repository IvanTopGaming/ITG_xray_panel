import base64
import binascii
import html
import json
import os
import time
from urllib.parse import quote, urlencode
import yaml
from flask import Blueprint, jsonify, request, Response
from app.extensions import limiter, db
from app.models import Client, Inbound, SystemSetting
from app.services import sub_cache


bp = Blueprint("subscription", __name__)


def _try_proxy_sub_to_child(uuid_str: str, req) -> Response | None:
    """If the UUID belongs to a client on a child panel, proxy the sub request there."""
    from app.models import LinkedPanel
    from app.services.panel_proxy import get_panel_snapshot

    for panel in LinkedPanel.query.filter_by(enable=True).all():
        snapshot = get_panel_snapshot(panel.id)
        if not snapshot:
            continue
        for ib_data in snapshot.get("inbounds", []):
            for c in ib_data.get("clients", []):
                if c.get("id") == uuid_str:
                    try:
                        ua = req.headers.get("User-Agent", "")
                        import requests as _req

                        resp = _req.get(
                            f"{panel.url.rstrip('/')}/api/sub/{uuid_str}",
                            headers={"User-Agent": ua},
                            timeout=8,
                        )
                        if resp.status_code == 200:
                            return Response(
                                resp.content,
                                status=200,
                                content_type=resp.headers.get("Content-Type", "text/plain"),
                                headers={
                                    k: v
                                    for k, v in resp.headers.items()
                                    if k.lower()
                                    in (
                                        "subscription-userinfo",
                                        "profile-update-interval",
                                        "profile-title",
                                        "content-disposition",
                                    )
                                },
                            )
                    except Exception:
                        pass
                    return None
    return None


def _get_remote_links_for_client(client_uuid: str, telegram_id: int | None) -> list[str]:
    """Fetch subscription links from child panels for clients matching this UUID or telegram_id."""
    from urllib.parse import urlparse
    from app.models import LinkedPanel
    from app.services.panel_proxy import get_panel_snapshot

    remote_links = []
    panels = LinkedPanel.query.filter_by(enable=True).all()
    if not panels:
        return remote_links

    for panel in panels:
        snapshot = get_panel_snapshot(panel.id)
        if not snapshot:
            continue
        try:
            panel_host = urlparse(panel.url).hostname or ""
        except Exception:
            continue
        if not panel_host:
            continue

        for ib_data in snapshot.get("inbounds", []):
            for c in ib_data.get("clients", []):
                if c.get("id") != client_uuid and (not telegram_id or c.get("telegram_id") != telegram_id):
                    continue
                if not c.get("enable", True):
                    continue
                remote_links.extend(_build_remote_link(panel_host, ib_data, c))
    return remote_links


def _build_remote_link(host: str, ib_data: dict, client_data: dict) -> list[str]:
    """Build protocol link for a remote inbound+client using the child panel's hostname."""
    protocol = ib_data.get("protocol", "")
    port = ib_data.get("port", 443)
    stream = ib_data.get("stream_settings", {})
    if isinstance(stream, str):
        try:
            stream = json.loads(stream)
        except Exception:
            stream = {}
    uuid = client_data.get("id", "")
    label = ib_data.get("label") or ib_data.get("tag", "remote")
    flow = client_data.get("flow", "")

    if protocol == "vless":
        query = {"type": stream.get("network", "tcp"), "security": stream.get("security", "none")}
        if stream.get("security") == "reality":
            rs = stream.get("realitySettings", {})
            query["pbk"] = _normalize_reality_public_key(rs.get("publicKey", ""))
            query["fp"] = rs.get("fingerprint", "chrome")
            query["sni"] = rs.get("serverNames", ["google.com"])[0] if rs.get("serverNames") else "google.com"
            query["sid"] = rs.get("shortIds", [""])[0] if rs.get("shortIds") else ""
            spx = rs.get("spiderX", "")
            if spx:
                query["spx"] = spx
        elif stream.get("security") == "tls":
            sni = stream.get("tlsSettings", {}).get("serverName", "")
            if sni:
                query["sni"] = sni
        if flow:
            query["flow"] = flow
        return [f"vless://{quote(str(uuid), safe='')}@{host}:{port}?{urlencode(query)}#{quote(label, safe='')}"]

    if protocol == "vmess":
        v_conf = {
            "v": "2",
            "ps": label,
            "add": host,
            "port": port,
            "id": uuid,
            "aid": "0",
            "net": stream.get("network", "tcp"),
            "type": "none",
            "host": "",
            "path": "",
            "tls": stream.get("security", "none"),
        }
        return [f"vmess://{base64.b64encode(json.dumps(v_conf).encode()).decode()}"]

    if protocol == "trojan":
        query = {"security": stream.get("security", "none"), "type": stream.get("network", "tcp")}
        if stream.get("security") == "reality":
            rs = stream.get("realitySettings", {})
            query["pbk"] = _normalize_reality_public_key(rs.get("publicKey", ""))
            query["fp"] = rs.get("fingerprint", "chrome")
            query["sni"] = rs.get("serverNames", ["google.com"])[0] if rs.get("serverNames") else "google.com"
            query["sid"] = rs.get("shortIds", [""])[0] if rs.get("shortIds") else ""
        return [f"trojan://{quote(str(uuid), safe='')}@{host}:{port}?{urlencode(query)}#{quote(label, safe='')}"]

    if protocol == "shadowsocks":
        method = stream.get("ssMethod", "chacha20-poly1305")
        server_pass = str(stream.get("ssPassword", "") or "")
        user_pass = str(uuid)
        if _is_ss2022_method(method):
            server_pass = _normalize_ss2022_key(server_pass)
            user_pass = _normalize_ss2022_key(user_pass)
        user_part = f"{method}:{server_pass}:{user_pass}" if _is_ss2022_method(method) else f"{method}:{user_pass}"
        return [f"ss://{base64.b64encode(user_part.encode()).decode()}@{host}:{port}#{quote(label, safe='')}"]

    return []


WARN_REMARK = {
    "unsupported": "⚠ Use Happ / v2RayTun / Shadowrocket — client unsupported",
    "limit": "⚠ Device limit reached — open subscription page",
}


def _warn_v2ray(state: str) -> str:
    remark = quote(WARN_REMARK[state], safe="")
    link = f"vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?encryption=none#{remark}"
    return base64.b64encode(link.encode("utf-8")).decode("utf-8")


def _warn_clash(state: str) -> str:
    name = WARN_REMARK[state]
    return yaml.safe_dump(
        {
            "proxies": [
                {
                    "name": name,
                    "type": "vless",
                    "server": "127.0.0.1",
                    "port": 1,
                    "uuid": "00000000-0000-0000-0000-000000000000",
                    "network": "tcp",
                }
            ],
            "proxy-groups": [{"name": "PROXY", "type": "select", "proxies": [name]}],
            "rules": ["MATCH,PROXY"],
        },
        allow_unicode=True,
        sort_keys=False,
    )


def _warn_singbox(state: str) -> str:
    name = WARN_REMARK[state]
    return json.dumps(
        {
            "outbounds": [
                {
                    "type": "vless",
                    "tag": name,
                    "server": "127.0.0.1",
                    "server_port": 1,
                    "uuid": "00000000-0000-0000-0000-000000000000",
                }
            ]
        },
        ensure_ascii=False,
    )


def _warn_response(state: str, user_agent: str, extra_headers: dict) -> Response:
    """Build the format-appropriate warn-config Response. Never cached."""
    if any(x in user_agent for x in ["clash", "meta", "stash"]):
        body = _warn_clash(state)
        return Response(
            body,
            mimetype="text/yaml",
            headers={
                "Content-Disposition": 'attachment; filename="config.yaml"',
                **_user_headers(),
                **extra_headers,
            },
        )
    if any(x in user_agent for x in ["sing-box", "nekobox"]):
        body = _warn_singbox(state)
        return Response(
            body,
            mimetype="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="config.json"',
                **_user_headers(),
                **extra_headers,
            },
        )
    body = _warn_v2ray(state)
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="config.txt"',
            **_user_headers(),
            **extra_headers,
        },
    )


def _config_filename(client, ext: str) -> str:
    """Filename for Content-Disposition.

    Personalised per client when available (extension dropped for cleaner
    display in client UIs — Content-Type header conveys the actual format).
    Warn-config (client=None) keeps the canonical extension so users can tell
    it's a system-generated message, not their real config.
    """
    if client is None or not client.email:
        return f"config.{ext}"
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in client.email)
    return safe or "config"


def _user_headers(client=None) -> dict:
    """Build standard subscription headers.

    With client: include subscription-userinfo (aggregate traffic + limit + expiry).
    Without client (warn-config path): only Profile-Update-Interval.
    Headers are recomputed every request — never cached.
    """
    setting = SystemSetting.query.filter_by(key="subscription_update_interval_hours").first()
    try:
        interval = int(setting.value) if setting and setting.value else 24
        if interval < 1:
            interval = 24
    except (ValueError, TypeError):
        interval = 24

    headers = {"Profile-Update-Interval": str(interval)}

    if client is None:
        return headers

    upload = int(client.up or 0)
    download = int(client.down or 0)
    total = int(client.limit_bytes or 0)
    expire_ms = int(client.expiry_time or 0)
    expire_s = expire_ms // 1000 if expire_ms else 0

    headers["subscription-userinfo"] = f"upload={upload}; download={download}; total={total}; expire={expire_s}"
    if client.email:
        headers["profile-title"] = client.email
    return headers


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
            proxy_resp = _try_proxy_sub_to_child(uuid_str, request)
            if proxy_resp is not None:
                return proxy_resp
            return "User not found", 404
        return Response(html, mimetype="text/html; charset=utf-8")

    # Device tracking gate — runs before cache lookup.
    client = db.session.get(Client, uuid_str)
    if not client or not client.enable:
        proxy_resp = _try_proxy_sub_to_child(uuid_str, request)
        if proxy_resp is not None:
            return proxy_resp
        return "User not found", 404
    inbound = Inbound.query.filter_by(tag=client.inbound_tag).first()
    if not inbound:
        return "User not found", 404

    from app.services.device_tracking import device_gate

    state, extra_headers = device_gate(
        client,
        inbound,
        {
            "x-hwid": request.headers.get("x-hwid", ""),
            "x-device-os": request.headers.get("x-device-os", ""),
            "x-ver-os": request.headers.get("x-ver-os", ""),
            "x-device-model": request.headers.get("x-device-model", ""),
            "user-agent": request.headers.get("User-Agent", ""),
            "_request_ip": (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()),
        },
    )
    if state != "ok":
        return _warn_response(state, user_agent, extra_headers)

    if any(x in user_agent for x in ["clash", "meta", "stash"]):
        cached = sub_cache.get("clash", uuid_str)
        if cached is not None:
            return Response(
                cached,
                mimetype="text/yaml",
                headers={
                    "Content-Disposition": f'attachment; filename="{_config_filename(client, "yaml")}"',
                    **_user_headers(client),
                    **extra_headers,
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
                "Content-Disposition": f'attachment; filename="{_config_filename(client, "yaml")}"',
                **_user_headers(client),
                **extra_headers,
            },
        )

    if any(x in user_agent for x in ["sing-box", "nekobox"]):
        cached = sub_cache.get("singbox", uuid_str)
        if cached is not None:
            return Response(
                cached,
                mimetype="application/json",
                headers={
                    "Content-Disposition": f'attachment; filename="{_config_filename(client, "json")}"',
                    **_user_headers(client),
                    **extra_headers,
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
                "Content-Disposition": f'attachment; filename="{_config_filename(client, "json")}"',
                **_user_headers(client),
                **extra_headers,
            },
        )

    cached = sub_cache.get("v2ray", uuid_str)
    if cached is not None:
        return Response(
            cached,
            mimetype="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{_config_filename(client, "txt")}"',
                **_user_headers(client),
                **extra_headers,
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
            "Content-Disposition": f'attachment; filename="{_config_filename(client, "txt")}"',
            **_user_headers(client),
            **extra_headers,
        },
    )


def get_subscription_content(uuid_str):
    """Local links + remote links from child panels for same user."""
    local = _get_local_subscription_content(uuid_str)
    client = db.session.get(Client, uuid_str)
    if not client:
        return local
    try:
        remote = _get_remote_links_for_client(uuid_str, None)
    except Exception:
        remote = []
    links = (local or []) + remote
    return links if links else None


def _get_local_subscription_content(uuid_str):
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
            f"{urlencode(query)}#{quote(ib.label or ib.tag, safe='')}"
        )
        return [link]

    elif ib.protocol == "vmess":
        v_conf = {
            "v": "2",
            "ps": ib.label or ib.tag,
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
            f"trojan://{quote(str(client.id), safe='')}@{host}:{ib.port}?{urlencode(query)}#{quote(ib.label or ib.tag, safe='')}"
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
        return [f"ss://{b64_user}@{host}:{ib.port}#{quote(ib.label or ib.tag, safe='')}"]

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

    all_proxies = [proxy_node]
    all_proxy_names = [proxy_node["name"]]

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

    all_outbounds = [outbound]

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

    used_total = int(client.up or 0) + int(client.down or 0)
    limit = int(client.limit_bytes or 0)

    # Build the absolute subscription URL.
    # - PANEL_BASE_URL (if set) is treated as a fully-formed prefix (may include secret path).
    # - Otherwise we reconstruct from request scheme/host and prepend PANEL_SECRET_PATH so
    #   the link works through Caddy (which 404s anything outside /<secret>/...).
    panel_base = os.getenv("PANEL_BASE_URL", "").rstrip("/")
    secret_path = os.getenv("PANEL_SECRET_PATH", "").strip("/")
    api_path = f"/api/sub/{quote(uuid_str, safe='')}"
    if panel_base:
        sub_url = f"{panel_base}{api_path}"
    else:
        sub_url = f"/{secret_path}{api_path}" if secret_path else api_path
    abs_sub_url = sub_url
    if not abs_sub_url.startswith("http"):
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme or "https")
        host = request.headers.get("X-Forwarded-Host", request.host)
        abs_sub_url = f"{scheme}://{host}{sub_url}"

    node_items = [{"name": "Master", "groups": [], "status": "online"}]

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

    from app.services.device_tracking import effective_device_limit, list_devices

    eff_limit = effective_device_limit(client, ib)
    # Show devices whenever any are recorded, even when limit is unlimited.
    devices = list_devices(client.id)

    def _device_icon(os_str):
        o = (os_str or "").lower()
        if "ios" in o or "android" in o:
            return "📱"
        if "mac" in o:
            return "💻"
        if "windows" in o or "linux" in o:
            return "🖥"
        return "⚙"

    devices_html_parts = []
    for d in devices:
        os_label = _esc(d.device_os or "Unknown")
        model = f" · {_esc(d.model)}" if d.model else ""
        os_ver = f"{_esc(d.os_ver)} · " if d.os_ver else ""
        devices_html_parts.append(
            f'<li class="device-row" data-id="{int(d.id)}">'
            f'<span class="dev-ic">{_device_icon(d.device_os)}</span>'
            f'<div class="dev-meta">'
            f'<div class="dev-name">{os_label}{model}</div>'
            f'<div class="dev-sub">{os_ver}'
            f'added <span class="ts" data-ts="{int(d.first_seen)}"></span>'
            f' · seen <span class="ts" data-ts="{int(d.last_seen)}"></span>'
            f"</div></div>"
            f'<button class="rev" onclick="revokeDevice({int(d.id)})">Revoke</button>'
            f"</li>"
        )

    if devices_html_parts:
        devices_inner = "".join(devices_html_parts)
    elif eff_limit > 0:
        devices_inner = (
            '<li class="empty">No devices yet — add this subscription URL '
            "in Happ / v2RayTun / Shadowrocket to register your first device.</li>"
        )
    else:
        devices_inner = ""

    if devices_inner:
        counter = f"{len(devices)} / {eff_limit} connected" if eff_limit > 0 else f"{len(devices)} connected"
        devices_card = (
            '<div class="card">'
            "<h2>Your devices</h2>"
            f'<div class="dev-counter">{counter}</div>'
            f'<ul class="devices">{devices_inner}</ul>'
            "</div>"
        )
    else:
        devices_card = ""

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
  ul.devices {{ list-style: none; padding: 0; margin: 0; }}
  ul.devices li {{
    display: flex; align-items: center; gap: 12px;
    padding: 10px 0; border-top: 1px solid rgba(255,255,255,0.05);
    transition: opacity .25s ease, transform .25s ease;
  }}
  ul.devices li:first-child {{ border-top: none; }}
  ul.devices li.empty {{ color: var(--muted); font-size: 13px; padding: 12px 0; }}
  .dev-ic {{ font-size: 22px; }}
  .dev-meta {{ flex: 1; }}
  .dev-name {{ font-weight: 500; font-size: 14px; }}
  .dev-sub  {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  .dev-counter {{ color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
  button.rev {{
    background: rgba(239,68,68,0.15); color: #f87171;
    border: 1px solid rgba(239,68,68,0.3); padding: 6px 12px;
    font-size: 12px; border-radius: 8px; cursor: pointer;
  }}
  button.rev:hover {{ background: rgba(239,68,68,0.25); }}
  button.rev[disabled] {{ opacity: .5; cursor: not-allowed; }}
  .device-row.removing {{ opacity: 0; transform: translateX(8px); }}
  .modal {{
    position: fixed; inset: 0; display: none; align-items: center; justify-content: center;
    z-index: 1000;
  }}
  .modal.show {{ display: flex; }}
  .modal-backdrop {{
    position: absolute; inset: 0; background: rgba(0,0,0,0.6);
    backdrop-filter: blur(4px);
  }}
  .modal-card {{
    position: relative; max-width: 380px; width: calc(100% - 32px);
    background: #1a1a26; border: 1px solid var(--border);
    border-radius: 18px; padding: 24px 24px 20px;
    display: flex; flex-direction: column; align-items: center; text-align: center;
    box-shadow: 0 10px 40px rgba(0,0,0,0.5);
  }}
  .modal-icon {{
    width: 64px; height: 64px; border-radius: 50%;
    background: rgba(239,68,68,0.1); color: #ef4444;
    display: flex; align-items: center; justify-content: center;
    margin-bottom: 16px;
  }}
  .modal-card h3 {{ margin: 0 0 8px; font-size: 17px; font-weight: 600; }}
  .modal-card p {{
    margin: 0 0 20px; color: rgba(229,231,235,0.85); font-size: 14px; line-height: 1.5;
  }}
  .modal-actions {{ display: flex; gap: 10px; width: 100%; }}
  .btn-secondary, .btn-danger {{
    flex: 1; padding: 10px 14px; font-size: 13px; font-weight: 600;
    border-radius: 10px; cursor: pointer; border: 1px solid transparent;
    transition: background .15s, opacity .15s;
  }}
  .btn-secondary {{
    background: rgba(255,255,255,0.06); color: var(--text);
    border-color: var(--border);
  }}
  .btn-secondary:hover {{ background: rgba(255,255,255,0.1); }}
  .btn-danger {{
    background: linear-gradient(135deg, #ef4444, #dc2626); color: #fff;
  }}
  .btn-danger:hover {{ filter: brightness(1.1); }}
  .btn-danger[disabled], .btn-secondary[disabled] {{ opacity: .55; cursor: not-allowed; }}
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
      <div class="row"><span class="label">Used</span><span class="val">{_esc(_format_bytes(used_total))}</span></div>
      {('<div class="row"><span class="label">Limit</span><span class="val">' + _esc(_format_bytes(limit)) + "</span></div>" + _bar(used_total, limit)) if limit > 0 else ""}
      <div class="row"><span class="label">Expires</span><span class="val">{_esc(_format_expiry(client.expiry_time))}</span></div>
    </div>

    <div class="card">
      <h2>Servers</h2>
      <ul class="nodes">{nodes_html}</ul>
    </div>

    {devices_card}
  </div>
  <div class="toast" id="toast">Copied to clipboard</div>
  <div class="modal" id="revokeModal" aria-hidden="true">
    <div class="modal-backdrop" onclick="closeRevokeModal()"></div>
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="revokeTitle">
      <div class="modal-icon"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></div>
      <h3 id="revokeTitle">Revoke device</h3>
      <p id="revokeBody">Revoke this device? It will need to re-register on the next subscription fetch.</p>
      <div class="modal-actions">
        <button type="button" class="btn-secondary" onclick="closeRevokeModal()">Cancel</button>
        <button type="button" class="btn-danger" id="revokeConfirmBtn" onclick="confirmRevoke()">Revoke</button>
      </div>
    </div>
  </div>
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
function timeAgo(ms) {{
  if (!ms) return 'never';
  var diff = Date.now() - ms;
  if (diff < 60000) return 'just now';
  if (diff < 3600000) return Math.floor(diff/60000) + 'm ago';
  if (diff < 86400000) return Math.floor(diff/3600000) + 'h ago';
  if (diff < 30 * 86400000) return Math.floor(diff/86400000) + 'd ago';
  return new Date(ms).toLocaleDateString();
}}
document.querySelectorAll('.ts').forEach(function(el){{
  el.textContent = timeAgo(parseInt(el.dataset.ts, 10));
}});
var pendingRevokeId = null;
function revokeDevice(id) {{
  pendingRevokeId = id;
  var row = document.querySelector('.device-row[data-id="' + id + '"]');
  var label = 'this device';
  if (row) {{
    var name = row.querySelector('.dev-name');
    if (name && name.textContent.trim()) label = name.textContent.trim();
  }}
  document.getElementById('revokeBody').textContent =
    'Revoke "' + label + '"? It will need to re-register on the next subscription fetch.';
  document.getElementById('revokeConfirmBtn').disabled = false;
  document.getElementById('revokeConfirmBtn').textContent = 'Revoke';
  document.getElementById('revokeModal').classList.add('show');
}}
function closeRevokeModal() {{
  document.getElementById('revokeModal').classList.remove('show');
  pendingRevokeId = null;
}}
function confirmRevoke() {{
  if (pendingRevokeId == null) return;
  var id = pendingRevokeId;
  var row = document.querySelector('.device-row[data-id="' + id + '"]');
  var rowBtn = row && row.querySelector('button.rev');
  var modalBtn = document.getElementById('revokeConfirmBtn');
  modalBtn.disabled = true; modalBtn.textContent = 'Revoking...';
  if (rowBtn) {{ rowBtn.disabled = true; rowBtn.textContent = 'Revoking...'; }}
  var url = window.location.pathname + '/devices/' + id;
  fetch(url, {{ method: 'DELETE' }}).then(function(r){{
    if (r.status === 204) {{
      row.classList.add('removing');
      setTimeout(function(){{
        row.remove();
        var rest = document.querySelectorAll('.device-row').length;
        var counter = document.querySelector('.dev-counter');
        if (counter) {{
          var parts = counter.textContent.split('/');
          counter.textContent = rest + (parts.length > 1 ? ' /' + parts[1] : ' connected');
        }}
      }}, 250);
      closeRevokeModal();
    }} else {{
      showToast('Failed, try again');
      modalBtn.disabled = false; modalBtn.textContent = 'Revoke';
      if (rowBtn) {{ rowBtn.disabled = false; rowBtn.textContent = 'Revoke'; }}
    }}
  }}).catch(function(){{
    showToast('Network error');
    modalBtn.disabled = false; modalBtn.textContent = 'Revoke';
    if (rowBtn) {{ rowBtn.disabled = false; rowBtn.textContent = 'Revoke'; }}
  }});
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape' && document.getElementById('revokeModal').classList.contains('show')) {{
    closeRevokeModal();
  }}
}});
</script>
</body>
</html>"""


@bp.route("/sub/<path:uuid_str>/devices", methods=["GET"])
@limiter.limit("60 per minute")
def sub_list_devices(uuid_str):
    client = db.session.get(Client, uuid_str)
    if not client:
        return jsonify({"error": "Not found"}), 404
    from app.services.device_tracking import list_devices

    devices = list_devices(uuid_str)
    return jsonify([d.to_dict(include_admin_fields=False) for d in devices])


@bp.route("/sub/<path:uuid_str>/devices/<int:device_id>", methods=["DELETE"])
@limiter.limit("30 per minute")
def sub_revoke_device(uuid_str, device_id):
    client = db.session.get(Client, uuid_str)
    if not client:
        return jsonify({"error": "Not found"}), 404
    from app.services.device_tracking import revoke_device

    if not revoke_device(uuid_str, device_id):
        return jsonify({"error": "Not found"}), 404
    try:
        sub_cache.invalidate_user(uuid_str)
    except Exception:
        pass
    return ("", 204)
