import base64
import binascii
import html
import json
import os
import time
from urllib.parse import quote, urlencode
import yaml
from flask import Blueprint, jsonify, request, Response
from panel_core.extensions import limiter, db
from panel_core.models import Client, Inbound, SystemSetting, TelegramUser
from panel_core.services import sub_cache
from panel_core.services.sub_links import build_aggregate_sub_url  # noqa: F401 — re-exported under the original name
from panel_core.xray.protocol import stream_supports_vless_flow


bp = Blueprint("subscription", __name__)


def _try_proxy_sub_to_child(uuid_str: str, req) -> Response | None:

    from panel_core.models import LinkedPanel
    from panel_core.services.panel_proxy import get_panel_snapshot

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
                            allow_redirects=False,
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

    from urllib.parse import urlparse
    from panel_core.models import LinkedPanel
    from panel_core.services.panel_proxy import get_panel_snapshot

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


def _remote_clients_for_headers(telegram_id):

    from types import SimpleNamespace
    from panel_core.models import LinkedPanel
    from panel_core.services.panel_proxy import get_panel_snapshot

    out = []
    if not telegram_id:
        return out
    try:
        for panel in LinkedPanel.query.filter_by(enable=True).all():
            snapshot = get_panel_snapshot(panel.id)
            if not snapshot:
                continue
            for ib_data in snapshot.get("inbounds", []):
                for c in ib_data.get("clients", []):
                    if c.get("telegram_id") != telegram_id or not c.get("enable", True):
                        continue
                    out.append(
                        SimpleNamespace(
                            up=int(c.get("up", 0) or 0),
                            down=int(c.get("down", 0) or 0),
                            limit_bytes=int(c.get("limit_bytes", 0) or 0),
                            expiry_time=int(c.get("expiry_time", 0) or 0),
                        )
                    )
    except Exception:
        pass
    return out


def _build_share_links(host, protocol, port, stream, client_id, flow, label) -> list[str]:

    if not isinstance(stream, dict):
        stream = {}
    network = stream.get("network", "tcp")
    security = stream.get("security", "none")
    uuid = quote(str(client_id), safe="")
    remark = quote(str(label), safe="")

    def _add_transport(query):
        if network == "grpc":
            query["serviceName"] = stream.get("grpcSettings", {}).get("serviceName", "grpc")
        else:
            t_path, t_host = _extract_transport_path_host(stream)
            if t_path:
                query["path"] = t_path
            if t_host:
                query["host"] = t_host

    def _add_reality(query):
        rs = stream.get("realitySettings", {}) or {}
        query["pbk"] = _normalize_reality_public_key(rs.get("publicKey", ""))
        query["fp"] = rs.get("fingerprint", "chrome")
        query["sni"] = (rs.get("serverNames") or ["google.com"])[0]
        query["sid"] = (rs.get("shortIds") or [""])[0]
        spx = rs.get("spiderX", "")
        if spx:
            query["spx"] = spx

    def _add_tls(query):
        sni = _extract_tls_server_name(stream)
        if sni:
            query["sni"] = sni
        alpn = _extract_tls_alpn(stream)
        if alpn:
            query["alpn"] = ",".join(alpn)
        fp = _extract_tls_utls_fingerprint(stream)
        if fp:
            query["fp"] = fp

    if protocol == "vless":
        query = {"type": network, "security": security}
        _add_transport(query)
        if security == "reality":
            _add_reality(query)
        elif security == "tls":
            _add_tls(query)
        if flow and stream_supports_vless_flow(stream):
            query["flow"] = flow
        return [f"vless://{uuid}@{host}:{port}?{urlencode(query)}#{remark}"]

    if protocol == "vmess":
        if network == "grpc":
            v_path, v_host = stream.get("grpcSettings", {}).get("serviceName", ""), ""
        else:
            v_path, v_host = _extract_transport_path_host(stream)
        v_conf = {
            "v": "2",
            "ps": str(label),
            "add": host,
            "port": port,
            "id": str(client_id),
            "aid": "0",
            "net": network,
            "type": "none",
            "host": v_host,
            "path": v_path,
            "tls": security,
        }
        if security == "tls":
            sni = _extract_tls_server_name(stream)
            if sni:
                v_conf["sni"] = sni
        return [f"vmess://{base64.b64encode(json.dumps(v_conf).encode()).decode()}"]

    if protocol == "trojan":
        query = {"security": security, "type": network}
        _add_transport(query)
        if security == "reality":
            _add_reality(query)
        elif security == "tls":
            _add_tls(query)
        return [f"trojan://{uuid}@{host}:{port}?{urlencode(query)}#{remark}"]

    if protocol == "shadowsocks":
        method = stream.get("ssMethod", "2022-blake3-aes-128-gcm")
        server_pass = str(stream.get("ssPassword", "") or "").strip()
        user_pass = str(client_id or "").strip()
        if _is_ss2022_method(method):
            server_pass = _normalize_ss2022_key(server_pass)
            user_pass = _normalize_ss2022_key(user_pass)
        user_part = f"{method}:{server_pass}:{user_pass}" if _is_ss2022_method(method) else f"{method}:{user_pass}"
        return [f"ss://{base64.b64encode(user_part.encode()).decode()}@{host}:{port}#{remark}"]

    return []


def _build_remote_link(host: str, ib_data: dict, client_data: dict) -> list[str]:

    stream = ib_data.get("stream_settings", {})
    if isinstance(stream, str):
        try:
            stream = json.loads(stream)
        except Exception:
            stream = {}
    return _build_share_links(
        host,
        ib_data.get("protocol", ""),
        ib_data.get("port", 443),
        stream,
        client_data.get("id", ""),
        client_data.get("flow", ""),
        ib_data.get("label") or ib_data.get("tag", "remote"),
    )


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

    if client is None or not client.email:
        return f"config.{ext}"
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in client.email)
    return safe or "config"


def _user_headers(client=None) -> dict:

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


def _aggregate_user_headers(clients) -> dict:

    setting = SystemSetting.query.filter_by(key="subscription_update_interval_hours").first()
    try:
        interval = int(setting.value) if setting and setting.value else 24
        if interval < 1:
            interval = 24
    except (ValueError, TypeError):
        interval = 24
    headers = {"Profile-Update-Interval": str(interval)}

    brand = SystemSetting.query.filter_by(key="brand_name").first()
    title = (brand.value if brand and brand.value else "Subscription").strip()[:25]
    if title.isascii():
        headers["profile-title"] = title
    else:
        headers["profile-title"] = "base64:" + base64.b64encode(title.encode("utf-8")).decode("ascii")

    clients = [c for c in clients if c is not None]
    if not clients:
        return headers

    limited = [c for c in clients if int(c.limit_bytes or 0) > 0]
    if limited:

        def remaining(c):
            return int(c.limit_bytes or 0) - (int(c.up or 0) + int(c.down or 0))

        pick = min(limited, key=remaining)
        upload = int(pick.up or 0)
        download = int(pick.down or 0)
        total = int(pick.limit_bytes or 0)
    else:
        upload = sum(int(c.up or 0) for c in clients)
        download = sum(int(c.down or 0) for c in clients)
        total = 0

    expiries = [int(c.expiry_time or 0) for c in clients if int(c.expiry_time or 0) > 0]
    expire_s = (min(expiries) // 1000) if expiries else 0

    headers["subscription-userinfo"] = f"upload={upload}; download={download}; total={total}; expire={expire_s}"
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
    if network not in ("ws", "xhttp", "httpupgrade", "splithttp"):
        return "", ""

    path = str(stream.get("wsPath", "") or "").strip()
    host = str(stream.get("wsHost", "") or "").strip()

    if not path or not host:
        nested_key = {
            "ws": "wsSettings",
            "xhttp": "xhttpSettings",
            "httpupgrade": "httpUpgradeSettings",
            "splithttp": "splitHttpSettings",
        }[network]
        sub = stream.get(nested_key, {})
        if isinstance(sub, dict):
            path = path or str(sub.get("path", "") or "")
            if network == "ws":
                headers = sub.get("headers", {})
                if isinstance(headers, dict):
                    host = host or str(headers.get("Host", "") or "")
            else:
                host = host or str(sub.get("host", "") or "")

    path = (path or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path, host.strip()


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

    return any(token in ua for token in ("mozilla", "applewebkit", "gecko", "trident", "edg"))


def _absolute_sub_url(token: str) -> str:
    sub_domain = os.getenv("SUB_DOMAIN", "").strip()
    if sub_domain:
        return f"https://{sub_domain}/api/sub/u/{token}"
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme or "https")
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}/api/sub/u/{token}"


@bp.route("/sub/u/<token>", methods=["GET"])
@limiter.limit("180 per minute")
def get_subscription_aggregate(token):
    user = TelegramUser.query.filter_by(sub_token=token).first()
    if not user or user.blocked:
        return "User not found", 404

    user_agent = request.headers.get("User-Agent", "").lower()
    forced_ua = (request.args.get("ua", "") or "").strip().lower()
    if forced_ua in ("clash", "meta", "stash"):
        user_agent = "clash"
    elif forced_ua in ("singbox", "sing-box", "nekobox"):
        user_agent = "sing-box"
    elif forced_ua in ("v2ray", "v2rayng", "raw"):
        user_agent = "v2ray"

    clients = Client.query.filter_by(telegram_id=user.telegram_id, enable=True).all()
    try:
        clients = clients + _remote_clients_for_headers(user.telegram_id)
    except Exception:
        pass
    headers = _aggregate_user_headers(clients)

    if _looks_like_browser(user_agent):
        lang = _pick_lang(request.args.get("lang", ""), request.headers.get("Accept-Language", ""))
        abs_sub_url = _absolute_sub_url(token)
        page = render_aggregate_subscription_page(user, lang, abs_sub_url)
        return Response(page, mimetype="text/html; charset=utf-8", headers=headers)

    from panel_core.services.device_tracking import user_device_gate

    gate_state, extra_headers = user_device_gate(
        user.telegram_id,
        {
            "x-hwid": request.headers.get("x-hwid", ""),
            "x-device-os": request.headers.get("x-device-os", ""),
            "x-ver-os": request.headers.get("x-ver-os", ""),
            "x-device-model": request.headers.get("x-device-model", ""),
            "user-agent": request.headers.get("User-Agent", ""),
            "_request_ip": (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()),
        },
    )
    if gate_state != "ok":
        return _warn_response(gate_state, user_agent, extra_headers)

    if any(x in user_agent for x in ["clash", "meta", "stash"]):
        cached = sub_cache.get("u-clash", token)
        if cached is None:
            cfg = generate_clash_config_for_user(user.telegram_id)
            if not cfg:
                return "User not found", 404
            sub_cache.set("u-clash", token, cfg)
            cached = cfg
        return Response(
            cached,
            mimetype="text/yaml",
            headers={"Content-Disposition": 'attachment; filename="config.yaml"', **headers, **extra_headers},
        )

    if any(x in user_agent for x in ["sing-box", "nekobox"]):
        cached = sub_cache.get("u-singbox", token)
        if cached is None:
            cfg = generate_singbox_config_for_user(user.telegram_id)
            if not cfg:
                return "User not found", 404
            sub_cache.set("u-singbox", token, cfg)
            cached = cfg
        return Response(
            cached,
            mimetype="application/json",
            headers={"Content-Disposition": 'attachment; filename="config.json"', **headers, **extra_headers},
        )

    cached = sub_cache.get("u-v2ray", token)
    if cached is None:
        links = get_subscription_content_for_user(user.telegram_id)
        if not links:
            return "User not found", 404
        cached = base64.b64encode("\n".join(links).encode("utf-8")).decode("utf-8")
        sub_cache.set("u-v2ray", token, cached)
    return Response(
        cached,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="config.txt"', **headers, **extra_headers},
    )


@bp.route("/sub/<path:uuid_str>", methods=["GET"])
@limiter.limit("180 per minute")
def get_subscription(uuid_str):
    user_agent = request.headers.get("User-Agent", "").lower()

    forced_ua = (request.args.get("ua", "") or "").strip().lower()
    if forced_ua in ("clash", "meta", "stash"):
        user_agent = "clash"
    elif forced_ua in ("singbox", "sing-box", "nekobox"):
        user_agent = "sing-box"
    elif forced_ua in ("v2ray", "v2rayng", "raw"):
        user_agent = "v2ray"

    client = db.session.get(Client, uuid_str)
    if not client or not client.enable:
        proxy_resp = _try_proxy_sub_to_child(uuid_str, request)
        if proxy_resp is not None:
            return proxy_resp
        return "User not found", 404
    inbound = Inbound.query.filter_by(tag=client.inbound_tag).first()
    if not inbound:
        return "User not found", 404

    from panel_core.services.device_tracking import device_gate

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


def _enabled_client_ids_for_user(telegram_id):

    rows = Client.query.filter_by(telegram_id=telegram_id, enable=True).with_entities(Client.id).all()
    return [r[0] for r in rows if r[0]]


def get_subscription_content_for_user(telegram_id):

    links = []
    for cid in _enabled_client_ids_for_user(telegram_id):
        local = _get_local_subscription_content(cid)
        if local:
            links.extend(local)
    try:
        remote = _get_remote_links_for_client(None, telegram_id)
    except Exception:
        remote = []
    links.extend(remote)
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
    return _build_share_links(host, ib.protocol, ib.port, stream, client.id, client.flow or "", ib.label or ib.tag)


def _remote_inbound_client_pairs(telegram_id):

    from urllib.parse import urlparse
    from panel_core.models import LinkedPanel
    from panel_core.services.panel_proxy import get_panel_snapshot

    if not telegram_id:
        return
    for panel in LinkedPanel.query.filter_by(enable=True).all():
        snapshot = get_panel_snapshot(panel.id)
        if not snapshot:
            continue
        host = ""
        try:
            host = urlparse(panel.url).hostname or ""
        except Exception:
            host = ""
        if not host:
            continue
        for ib_data in snapshot.get("inbounds", []):
            for c in ib_data.get("clients", []):
                if c.get("telegram_id") != telegram_id or not c.get("enable", True):
                    continue
                stream = ib_data.get("stream_settings", {})
                if isinstance(stream, str):
                    try:
                        stream = json.loads(stream)
                    except Exception:
                        stream = {}
                yield host, ib_data, c, stream


def _build_clash_proxy(name, protocol, host, port, stream, client_id, flow):

    if not isinstance(stream, dict):
        stream = {}
    security = stream.get("security", "none")
    node = {
        "name": name,
        "server": host,
        "port": port,
        "type": protocol if protocol != "shadowsocks" else "ss",
    }

    def _reality(n):
        r = stream.get("realitySettings", {}) or {}
        n["servername"] = (r.get("serverNames") or ["google.com"])[0]
        n["client-fingerprint"] = r.get("fingerprint", "chrome")
        n["reality-opts"] = {
            "public-key": _normalize_reality_public_key(r.get("publicKey", "")),
            "short-id": (r.get("shortIds") or [""])[0],
        }

    def _tls(n):
        sni = _extract_tls_server_name(stream)
        if sni:
            n["servername"] = sni
        fp = _extract_tls_utls_fingerprint(stream)
        if fp:
            n["client-fingerprint"] = fp

    if protocol == "vless":
        node.update({"uuid": client_id, "network": stream.get("network", "tcp"), "udp": True})
        if security in ("tls", "reality"):
            node["tls"] = True
        if security == "reality":
            _reality(node)
        elif security == "tls":
            _tls(node)
        if flow and stream_supports_vless_flow(stream):
            node["flow"] = flow
    elif protocol == "vmess":
        node.update(
            {
                "uuid": client_id,
                "alterId": 0,
                "cipher": "auto",
                "network": stream.get("network", "tcp"),
                "tls": security == "tls",
                "udp": True,
            }
        )
        if security == "tls":
            _tls(node)
    elif protocol == "trojan":
        node.update({"password": client_id, "network": stream.get("network", "tcp"), "udp": True})
        if security in ("tls", "reality"):
            node["tls"] = True
        if security == "reality":
            _reality(node)
        elif security == "tls":
            _tls(node)
    elif protocol == "shadowsocks":
        method = stream.get("ssMethod", "chacha20-poly1305")
        server_pass = str(stream.get("ssPassword", "") or "").strip()
        user_pass = str(client_id or "").strip()
        if _is_ss2022_method(method):
            server_pass = _normalize_ss2022_key(server_pass)
            user_pass = _normalize_ss2022_key(user_pass)
        node["cipher"] = method
        node["password"] = f"{server_pass}:{user_pass}" if _is_ss2022_method(method) else user_pass

    _apply_clash_transport(node, stream)
    return node


def _build_singbox_outbound(tag, protocol, host, port, stream, client_id, flow):

    if not isinstance(stream, dict):
        stream = {}
    security = stream.get("security", "none")
    ob = {"tag": tag, "server": host, "server_port": port, "type": protocol}

    def _tls():
        p = {"enabled": True}
        sni = _extract_tls_server_name(stream)
        if sni:
            p["server_name"] = sni
        alpn = _extract_tls_alpn(stream)
        if alpn:
            p["alpn"] = alpn
        fp = _extract_tls_utls_fingerprint(stream)
        if fp:
            p["utls"] = {"enabled": True, "fingerprint": fp}
        return p

    def _reality():
        r = stream.get("realitySettings", {}) or {}
        return {
            "enabled": True,
            "server_name": (r.get("serverNames") or ["google.com"])[0],
            "utls": {"enabled": True, "fingerprint": r.get("fingerprint", "chrome")},
            "reality": {
                "enabled": True,
                "public_key": _normalize_reality_public_key(r.get("publicKey", "")),
                "short_id": (r.get("shortIds") or [""])[0],
            },
        }

    if protocol == "vless":
        ob.update({"uuid": client_id, "packet_encoding": "xudp"})
        if flow and stream_supports_vless_flow(stream):
            ob["flow"] = flow
        if security == "reality":
            ob["tls"] = _reality()
        elif security == "tls":
            ob["tls"] = _tls()
    elif protocol == "vmess":
        ob.update({"uuid": client_id, "security": "auto"})
        if security == "tls":
            ob["tls"] = _tls()
    elif protocol == "trojan":
        ob["password"] = client_id
        if security == "reality":
            ob["tls"] = _reality()
        elif security == "tls":
            ob["tls"] = _tls()
    elif protocol == "shadowsocks":
        method = stream.get("ssMethod", "chacha20-poly1305")
        ob["method"] = method
        server_pass = str(stream.get("ssPassword", "") or "").strip()
        user_pass = str(client_id or "").strip()
        if _is_ss2022_method(method):
            server_pass = _normalize_ss2022_key(server_pass)
            user_pass = _normalize_ss2022_key(user_pass)
        ob["password"] = f"{server_pass}:{user_pass}" if _is_ss2022_method(method) else user_pass

    _apply_singbox_transport(ob, stream)
    return ob


def generate_clash_config(uuid_str):
    client = db.session.get(Client, uuid_str)
    if not client or not client.enable:
        return None
    ib = Inbound.query.filter_by(tag=client.inbound_tag).first()
    if not ib:
        return None
    stream = json.loads(ib.stream_settings)
    host = os.getenv("PANEL_DOMAIN", "localhost")
    proxy_node = _build_clash_proxy(
        f"{ib.tag}-{client.email}", ib.protocol, host, ib.port, stream, client.id, client.flow or ""
    )

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
    outbound = _build_singbox_outbound("proxy", ib.protocol, host, ib.port, stream, client.id, client.flow or "")

    all_outbounds = [outbound]

    config = {
        "log": {"level": "info", "timestamp": True},
        "dns": {
            "servers": [
                {"tag": "google", "type": "udp", "server": "8.8.8.8", "detour": "proxy"},
                {"tag": "local", "type": "local"},
            ]
        },
        "inbounds": [{"type": "tun", "tag": "tun-in", "address": ["172.19.0.1/30"], "auto_route": True}],
        "outbounds": all_outbounds + [{"type": "direct", "tag": "direct"}],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
            ],
            "final": "proxy",
            "auto_detect_interface": True,
            "default_domain_resolver": "local",
        },
    }
    return json.dumps(config, indent=2)


def generate_clash_config_for_user(telegram_id):

    proxies = []
    seen = set()

    for cid in _enabled_client_ids_for_user(telegram_id):
        client = db.session.get(Client, cid)
        if not client or not client.enable:
            continue
        ib = Inbound.query.filter_by(tag=client.inbound_tag).first()
        if not ib:
            continue
        name = f"{ib.tag}-{client.email}"
        if name in seen:
            continue
        seen.add(name)
        try:
            stream = json.loads(ib.stream_settings)
        except Exception:
            stream = {}
        proxies.append(
            _build_clash_proxy(
                name, ib.protocol, os.getenv("PANEL_DOMAIN", "localhost"), ib.port, stream, client.id, client.flow or ""
            )
        )

    for host, ib_data, c, stream in _remote_inbound_client_pairs(telegram_id):
        label = ib_data.get("label") or ib_data.get("tag", "remote")
        name = f"{label}-{c.get('email') or c.get('id', '')}"
        if name in seen:
            continue
        seen.add(name)
        try:
            proxies.append(
                _build_clash_proxy(
                    name,
                    ib_data.get("protocol", ""),
                    host,
                    ib_data.get("port", 443),
                    stream,
                    c.get("id", ""),
                    c.get("flow", ""),
                )
            )
        except Exception:
            pass

    if not proxies:
        return None
    names = [p["name"] for p in proxies]
    config = {
        "port": 7890,
        "socks-port": 7891,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "FASTEST",
                "type": "url-test",
                "url": "http://www.gstatic.com/generate_204",
                "interval": 300,
                "proxies": names,
            }
        ],
        "rules": ["GEOIP,CN,DIRECT", "MATCH,FASTEST"],
    }
    return yaml.dump(config, allow_unicode=True, sort_keys=False)


def generate_singbox_config_for_user(telegram_id):

    outbounds = []
    seen = set()

    for cid in _enabled_client_ids_for_user(telegram_id):
        client = db.session.get(Client, cid)
        if not client or not client.enable:
            continue
        ib = Inbound.query.filter_by(tag=client.inbound_tag).first()
        if not ib:
            continue
        tag = f"{ib.tag}-{client.email}"
        if tag in seen:
            continue
        seen.add(tag)
        try:
            stream = json.loads(ib.stream_settings)
        except Exception:
            stream = {}
        outbounds.append(
            _build_singbox_outbound(
                tag, ib.protocol, os.getenv("PANEL_DOMAIN", "localhost"), ib.port, stream, client.id, client.flow or ""
            )
        )

    for host, ib_data, c, stream in _remote_inbound_client_pairs(telegram_id):
        label = ib_data.get("label") or ib_data.get("tag", "remote")
        tag = f"{label}-{c.get('email') or c.get('id', '')}"
        if tag in seen:
            continue
        seen.add(tag)
        try:
            outbounds.append(
                _build_singbox_outbound(
                    tag,
                    ib_data.get("protocol", ""),
                    host,
                    ib_data.get("port", 443),
                    stream,
                    c.get("id", ""),
                    c.get("flow", ""),
                )
            )
        except Exception:
            pass

    if not outbounds:
        return None
    tags = [o["tag"] for o in outbounds]
    config = {
        "log": {"level": "info", "timestamp": True},
        "dns": {
            "servers": [
                {"tag": "google", "type": "udp", "server": "8.8.8.8", "detour": "PROXY"},
                {"tag": "local", "type": "local"},
            ]
        },
        "inbounds": [{"type": "tun", "tag": "tun-in", "address": ["172.19.0.1/30"], "auto_route": True}],
        "outbounds": [{"type": "selector", "tag": "PROXY", "outbounds": tags}]
        + outbounds
        + [{"type": "direct", "tag": "direct"}],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
            ],
            "final": "PROXY",
            "auto_detect_interface": True,
            "default_domain_resolver": "local",
        },
    }
    return json.dumps(config, indent=2)


def _protocol_tag(protocol, stream) -> str:

    if isinstance(stream, str):
        try:
            stream = json.loads(stream)
        except (TypeError, ValueError):
            stream = {}
    stream = stream or {}
    proto = (protocol or "").lower()
    network = (stream.get("network") or "tcp").lower()
    security = (stream.get("security") or "none").lower()

    if proto == "vless":
        if security == "reality":
            return "Reality"
        if network in ("ws", "websocket"):
            return "VLESS-WS"
        if network == "grpc":
            return "VLESS-gRPC"
        return "VLESS"
    if proto == "vmess":
        if network in ("ws", "websocket"):
            return "VMess-WS"
        return "VMess"
    if proto == "trojan":
        return "Trojan"
    if proto == "shadowsocks":
        return "Shadowsocks"
    if proto == "wireguard":
        return "WireGuard"
    return proto.upper() or "Proxy"


_NODE_ONLINE_WINDOW_MS = 5 * 60 * 1000


def _user_page_nodes(telegram_id):

    now_ms = int(time.time() * 1000)
    nodes = []

    clients = Client.query.filter_by(telegram_id=telegram_id).all()
    ib_by_tag = {}
    for c in clients:
        ib = ib_by_tag.get(c.inbound_tag)
        if ib is None:
            ib = Inbound.query.filter_by(tag=c.inbound_tag).first()
            ib_by_tag[c.inbound_tag] = ib
        if ib is None:
            continue
        used = int(c.up or 0) + int(c.down or 0)
        limit = int(c.limit_bytes or 0)
        last_seen = int(c.last_seen or 0)
        online = bool(c.enable) and last_seen > 0 and (now_ms - last_seen) <= _NODE_ONLINE_WINDOW_MS
        nodes.append(
            {
                "name": ib.label or ib.tag,
                "tag": _protocol_tag(ib.protocol, ib.stream_settings),
                "used": used,
                "limit": limit,
                "expiry": int(c.expiry_time or 0),
                "online": online,
                "enabled": bool(c.enable),
                "unlimited": limit <= 0,
            }
        )

    try:
        from panel_core.models import LinkedPanel
        from panel_core.services.panel_proxy import get_panel_snapshot

        for panel in LinkedPanel.query.filter_by(enable=True).all():
            snapshot = get_panel_snapshot(panel.id)
            if not snapshot:
                continue
            panel_online = (panel.status or "").lower() == "online"
            for ib_data in snapshot.get("inbounds", []):
                for c in ib_data.get("clients", []):
                    if c.get("telegram_id") != telegram_id:
                        continue
                    used = int(c.get("up", 0) or 0) + int(c.get("down", 0) or 0)
                    limit = int(c.get("limit_bytes", 0) or 0)
                    enabled = bool(c.get("enable", True))
                    nodes.append(
                        {
                            "name": ib_data.get("label") or ib_data.get("tag", "remote"),
                            "tag": _protocol_tag(ib_data.get("protocol", ""), ib_data.get("stream_settings", {})),
                            "used": used,
                            "limit": limit,
                            "expiry": int(c.get("expiry_time", 0) or 0),
                            "online": panel_online and enabled,
                            "enabled": enabled,
                            "unlimited": limit <= 0,
                        }
                    )
    except Exception:
        pass

    return nodes


def _user_device_summary(telegram_id):

    from panel_core.services.device_tracking import list_devices, subscription_device_settings

    enabled, limit = subscription_device_settings()
    if not enabled:
        return None
    clients = Client.query.filter_by(telegram_id=telegram_id, enable=True).all()
    hwids = set()
    for c in clients:
        for d in list_devices(c.id):
            if d.hwid:
                hwids.add(d.hwid)
    return {"count": len(hwids), "limit": limit}


def _subscription_info_payload(user, token) -> dict:
    brand_row = SystemSetting.query.filter_by(key="brand_name").first()
    brand = (brand_row.value if brand_row and brand_row.value else "").strip()

    nodes = _user_page_nodes(user.telegram_id)
    dev = _user_device_summary(user.telegram_id)

    interval_row = SystemSetting.query.filter_by(key="subscription_update_interval_hours").first()
    try:
        interval = int(interval_row.value) if interval_row and interval_row.value else 24
        if interval < 1:
            interval = 24
    except (ValueError, TypeError):
        interval = 24

    expiries = [n["expiry"] for n in nodes if n["enabled"] and n["expiry"] > 0] or [
        n["expiry"] for n in nodes if n["expiry"] > 0
    ]
    active = not user.blocked and any(n["enabled"] for n in nodes)

    return {
        "brand": brand,
        "sub_url": _absolute_sub_url(token),
        "status": "active" if active else "disabled",
        "expiry_at": min(expiries) if expiries else 0,
        "devices": None if dev is None else {"count": dev["count"], "limit": dev["limit"]},
        "nodes": [
            {
                "name": n["name"],
                "tag": n["tag"],
                "used": n["used"],
                "limit": n["limit"],
                "expiry": n["expiry"],
                "online": n["online"],
                "enabled": n["enabled"],
            }
            for n in nodes
        ],
        "update_interval_hours": interval,
    }


@bp.route("/sub/u/<token>/info", methods=["GET"])
@limiter.limit("180 per minute")
def get_subscription_info(token):
    user = TelegramUser.query.filter_by(sub_token=token).first()
    if not user or user.blocked:
        return "User not found", 404
    return jsonify(_subscription_info_payload(user, token))


_PAGE_STRINGS = {
    "en": {
        "default_brand": "Subscription",
        "status_active": "Active",
        "status_disabled": "Disabled",
        "hero_title": "Your subscription link",
        "copy": "Copy link",
        "hint": "Paste into Happ · v2RayTun · Streisand · Hiddify. Configs refresh themselves — no re-import when servers change.",
        "valid_until": "Valid until",
        "days_left": "{n} days left",
        "expired": "expired",
        "never": "no expiry",
        "devices": "Devices",
        "connected": "connected",
        "nodes": "Nodes",
        "of_gb": "of",
        "almost": "almost exhausted",
        "unlimited": "Unlimited · used",
        "until": "until",
        "download": "Download config",
        "auto_update": "Subscription updates automatically every {h} h",
        "no_nodes": "No nodes yet",
        "copied": "Copied to clipboard",
        "months": [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
    },
    "ru": {
        "default_brand": "Подписка",
        "status_active": "Активна",
        "status_disabled": "Отключена",
        "hero_title": "Ваша ссылка подписки",
        "copy": "Скопировать ссылку",
        "hint": "Вставьте в Happ · v2RayTun · Streisand · Hiddify. Конфиги обновятся сами — при смене серверов переимпортировать ничего не надо.",
        "valid_until": "Действует до",
        "days_left": "осталось {n} дн.",
        "expired": "истекла",
        "never": "бессрочно",
        "devices": "Устройства",
        "connected": "подключено",
        "nodes": "Узлы",
        "of_gb": "из",
        "almost": "почти исчерпан",
        "unlimited": "Безлимит · использовано",
        "until": "до",
        "download": "Скачать конфиг",
        "auto_update": "Подписка обновляется автоматически каждые {h} ч",
        "no_nodes": "Пока нет узлов",
        "copied": "Скопировано",
        "months": [
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ],
    },
}


def _format_date_localized(ms, lang) -> str:

    s = _PAGE_STRINGS.get(lang, _PAGE_STRINGS["en"])
    if not ms or ms <= 0:
        return s["never"]
    try:
        tm = time.localtime(ms / 1000)
    except (OSError, OverflowError, ValueError):
        return "—"
    return f"{tm.tm_mday} {s['months'][tm.tm_mon - 1]}"


def _pick_lang(query_lang, accept_language) -> str:

    q = (query_lang or "").strip().lower()[:2]
    if q in _PAGE_STRINGS:
        return q
    if q:
        return "en"
    al = (accept_language or "").strip().lower()
    for part in al.split(","):
        code = part.split(";")[0].strip()[:2]
        if code in _PAGE_STRINGS:
            return code
    return "en"


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


def render_aggregate_subscription_page(user, lang, abs_sub_url):

    s = _PAGE_STRINGS.get(lang, _PAGE_STRINGS["en"])

    brand_row = SystemSetting.query.filter_by(key="brand_name").first()
    brand = (brand_row.value if brand_row and brand_row.value else "").strip() or s["default_brand"]

    nodes = _user_page_nodes(user.telegram_id)
    dev = _user_device_summary(user.telegram_id)

    interval_row = SystemSetting.query.filter_by(key="subscription_update_interval_hours").first()
    try:
        interval = int(interval_row.value) if interval_row and interval_row.value else 24
        if interval < 1:
            interval = 24
    except (ValueError, TypeError):
        interval = 24

    active = not user.blocked and any(n["enabled"] for n in nodes)
    status_pill = (
        f'<span class="pill">● {html.escape(s["status_active"])}</span>'
        if active
        else f'<span class="pill off">● {html.escape(s["status_disabled"])}</span>'
    )

    now_ms = int(time.time() * 1000)
    expiries = [n["expiry"] for n in nodes if n["enabled"] and n["expiry"] > 0] or [
        n["expiry"] for n in nodes if n["expiry"] > 0
    ]
    if expiries:
        nearest = min(expiries)
        until_str = _format_date_localized(nearest, lang)
        if nearest <= now_ms:
            days_sub = s["expired"]
        else:
            days = -(-(nearest - now_ms) // 86400_000)
            days_sub = s["days_left"].format(n=days)
    else:
        until_str = s["never"]
        days_sub = ""

    def _esc(x):
        return html.escape(str(x or ""))

    summary_boxes = [
        f'<div class="sbox"><div class="small">{_esc(s["valid_until"])}</div>'
        f'<div class="big">{_esc(until_str)}</div>'
        f'<div class="small" style="margin-top:3px">{_esc(days_sub)}</div></div>'
    ]
    if dev is not None:
        if dev["limit"] > 0:
            dev_big = f'{dev["count"]} <span style="font-size:14px;color:#9a90ad">/ {dev["limit"]}</span>'
        else:
            dev_big = f"{dev['count']}"
        summary_boxes.append(
            f'<div class="sbox"><div class="small">{_esc(s["devices"])}</div>'
            f'<div class="big">{dev_big}</div>'
            f'<div class="small" style="margin-top:3px">{_esc(s["connected"])}</div></div>'
        )
    summary_html = '<div class="summary">' + "".join(summary_boxes) + "</div>"

    node_parts = []
    for n in nodes:
        dot = "on" if n["online"] else "off"
        disabled_cls = " disabled" if not n["enabled"] else ""
        head = (
            f'<div class="node-head"><span class="dot {dot}"></span>'
            f'<span class="node-name">{_esc(n["name"])}</span>'
            f'<span class="tag">{_esc(n["tag"])}</span></div>'
        )
        until_node = f"{_esc(s['until'])} {_esc(_format_date_localized(n['expiry'], lang))}" if n["expiry"] > 0 else ""
        if n["unlimited"]:
            body = (
                f'<div class="unlim"><span>∞ {_esc(s["unlimited"])} '
                f"<b>{_esc(_format_bytes(n['used']))}</b></span><span>{until_node}</span></div>"
            )
        else:
            pct = min(100.0, n["used"] * 100.0 / n["limit"]) if n["limit"] > 0 else 0.0
            warn = pct >= 90.0
            fill_cls = "bar-fill warn" if warn else "bar-fill"
            meta_left = f"{_esc(_format_bytes(n['used']))} {_esc(s['of_gb'])} {_esc(_format_bytes(n['limit']))}"
            if warn:
                meta_left = f'<span style="color:#fbbf24">{meta_left} · {_esc(s["almost"])}</span>'
            else:
                meta_left = f"<span>{meta_left}</span>"
            body = (
                f'<div class="bar"><div class="{fill_cls}" style="width:{pct:.0f}%"></div></div>'
                f'<div class="node-meta">{meta_left}<span>{until_node}</span></div>'
            )
        node_parts.append(f'<div class="node{disabled_cls}">{head}{body}</div>')
    nodes_html = "".join(node_parts) or f'<div class="node">{_esc(s["no_nodes"])}</div>'

    return f"""<!doctype html>
<html lang="{_esc(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(brand)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Roboto', sans-serif;
    background:
      radial-gradient(circle at 15% 15%, rgba(46,16,101,0.40) 0%, transparent 40%),
      radial-gradient(circle at 85% 85%, rgba(76,29,149,0.30) 0%, transparent 40%),
      #050505;
    color: #eae6f0; min-height: 100vh; padding: 32px 16px 64px; -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 720px; margin: 0 auto; }}
  .top {{ display:flex; align-items:center; justify-content:space-between; margin-bottom: 28px; }}
  .brand {{ display:flex; align-items:center; gap:13px; }}
  .glyph {{ width:42px; height:42px; border-radius:13px; background:linear-gradient(135deg,rgba(208,188,255,0.18),rgba(124,77,255,0.12));
    border:1px solid rgba(208,188,255,0.25); display:flex; align-items:center; justify-content:center; font-size:20px; box-shadow:0 0 22px rgba(208,188,255,0.18); }}
  .brand h1 {{ font-size:19px; font-weight:700; letter-spacing:.3px; }}
  .pill {{ font-size:12px; font-weight:500; padding:6px 13px; border-radius:999px; background:rgba(126,231,135,0.12); color:#7ee787; border:1px solid rgba(126,231,135,0.25); }}
  .pill.off {{ background:rgba(239,68,68,0.12); color:#f87171; border-color:rgba(239,68,68,0.25); }}
  .card {{ background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.06); border-radius:20px; padding:22px; margin-bottom:16px; backdrop-filter:blur(12px); }}
  .card h2 {{ font-size:13px; font-weight:500; text-transform:uppercase; letter-spacing:1px; color:#9a90ad; margin-bottom:16px; }}
  .hero {{ background:linear-gradient(135deg,rgba(79,55,139,0.35),rgba(124,77,255,0.12)); border-color:rgba(208,188,255,0.18); }}
  .hero h2 {{ color:#d8c9ff; }}
  .url-box {{ font-family:'Roboto Mono',monospace; font-size:13px; color:#d8c9ff; background:rgba(0,0,0,0.35);
    border:1px solid rgba(208,188,255,0.15); border-radius:12px; padding:14px 16px; word-break:break-all; margin-bottom:13px; }}
  .btn {{ border:none; cursor:pointer; font-family:inherit; font-weight:500; font-size:14px; padding:12px 20px; border-radius:12px; display:inline-flex; align-items:center; gap:8px; transition:transform .12s,box-shadow .2s; }}
  .btn-primary {{ background:linear-gradient(135deg,#D0BCFF,#7c4dff); color:#1a1228; box-shadow:0 0 18px rgba(208,188,255,0.25); width:100%; justify-content:center; }}
  .btn-primary:hover {{ transform:translateY(-1px); box-shadow:0 0 26px rgba(208,188,255,0.45); }}
  .hint {{ font-size:12px; color:#9a90ad; margin-top:13px; }}
  .summary {{ display:flex; gap:14px; margin-bottom:16px; }}
  .sbox {{ flex:1; background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.06); border-radius:16px; padding:16px 18px; }}
  .sbox .small {{ font-size:12px; color:#9a90ad; }}
  .sbox .big {{ font-size:22px; font-weight:700; color:#fff; margin-top:5px; }}
  .node {{ padding:16px 0; border-bottom:1px solid rgba(255,255,255,0.05); }}
  .node:first-of-type {{ padding-top:0; }}
  .node:last-child {{ border-bottom:none; padding-bottom:0; }}
  .node-head {{ display:flex; align-items:center; gap:10px; margin-bottom:11px; }}
  .dot {{ width:9px; height:9px; border-radius:50%; flex-shrink:0; }}
  .dot.on {{ background:#7ee787; box-shadow:0 0 8px #7ee787; }}
  .dot.off {{ background:#6f6781; }}
  .node-name {{ font-weight:500; font-size:15px; flex:1; }}
  .node.disabled {{ opacity:.42; }}
  .tag {{ font-size:11px; padding:3px 9px; border-radius:7px; background:rgba(208,188,255,0.12); color:#cabfe0; border:1px solid rgba(208,188,255,0.18); }}
  .bar {{ height:7px; border-radius:999px; background:rgba(255,255,255,0.08); overflow:hidden; }}
  .bar-fill {{ height:100%; border-radius:999px; background:linear-gradient(90deg,#7c4dff,#D0BCFF); box-shadow:0 0 10px rgba(208,188,255,0.35); }}
  .bar-fill.warn {{ background:linear-gradient(90deg,#f59e0b,#fbbf24); box-shadow:0 0 10px rgba(245,158,11,0.4); }}
  .node-meta {{ display:flex; justify-content:space-between; font-size:12px; color:#9a90ad; margin-top:8px; }}
  .unlim {{ font-size:12px; color:#9a90ad; display:flex; justify-content:space-between; }}
  .unlim b {{ color:#cabfe0; font-weight:500; }}
  .foot {{ text-align:center; font-size:12px; color:#6f6781; margin-top:22px; line-height:1.8; }}
  .foot a {{ color:#9a90ad; text-decoration:none; border-bottom:1px dotted #555; }}
  .toast {{ position:fixed; bottom:24px; left:50%; transform:translateX(-50%); background:#1a1a26; border:1px solid rgba(255,255,255,0.08); padding:10px 16px; border-radius:999px; font-size:13px; opacity:0; transition:opacity .25s; }}
  .toast.show {{ opacity:1; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand"><div class="glyph">🔑</div><h1>{_esc(brand)}</h1></div>
    {status_pill}
  </div>

  <div class="card hero">
    <h2>{_esc(s["hero_title"])}</h2>
    <div class="url-box" id="suburl">{_esc(abs_sub_url)}</div>
    <button class="btn btn-primary" onclick="copySub()">⧉ {_esc(s["copy"])}</button>
    <div class="hint">{_esc(s["hint"])}</div>
  </div>

  {summary_html}

  <div class="card">
    <h2>{_esc(s["nodes"])} · {len(nodes)}</h2>
    {nodes_html}
  </div>

  <div class="foot">
    {_esc(s["download"])}: <a href="{_esc(abs_sub_url)}?ua=v2ray" download="config.txt">v2ray</a> ·
    <a href="{_esc(abs_sub_url)}?ua=clash" download="config.yaml">Clash</a> ·
    <a href="{_esc(abs_sub_url)}?ua=singbox" download="config.json">sing-box</a><br>
    {_esc(s["auto_update"].format(h=interval))}
  </div>
</div>
<div class="toast" id="toast">{_esc(s["copied"])}</div>
<script>
function copySub() {{
  var url = document.getElementById('suburl').textContent.trim();
  function done() {{ var t = document.getElementById('toast'); t.classList.add('show'); setTimeout(function() {{ t.classList.remove('show'); }}, 1600); }}
  if (navigator.clipboard) {{ navigator.clipboard.writeText(url).then(done); }}
  else {{ var ta=document.createElement('textarea'); ta.value=url; document.body.appendChild(ta); ta.select(); try {{ document.execCommand('copy'); done(); }} catch(e) {{}} document.body.removeChild(ta); }}
}}
</script>
</body>
</html>"""
