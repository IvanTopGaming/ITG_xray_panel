import base64
import binascii
import json
import os
import time
from urllib.parse import quote, urlencode
import yaml
from flask import Blueprint, jsonify, request, Response, send_from_directory
from panel_core.extensions import limiter, db
from panel_core.models import Client, Inbound, SystemSetting, TelegramUser
from panel_core.services import sub_cache
from panel_core.services.sub_links import build_aggregate_sub_url  # noqa: F401 — re-exported under the original name
from panel_core.xray.protocol import stream_supports_vless_flow


bp = Blueprint("subscription", __name__)


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


def _filename_from_email(email, ext: str) -> str:

    if not email:
        return f"config.{ext}"
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(email))
    return safe or "config"


def _config_filename(client, ext: str) -> str:

    return _filename_from_email(None if client is None else client.email, ext)


def _update_interval_hours() -> int:

    setting = SystemSetting.query.filter_by(key="subscription_update_interval_hours").first()
    try:
        interval = int(setting.value) if setting and setting.value else 24
        if interval < 1:
            interval = 24
    except (ValueError, TypeError):
        interval = 24
    return interval


def _resolve_user_agent() -> str:

    user_agent = request.headers.get("User-Agent", "").lower()
    forced_ua = (request.args.get("ua", "") or "").strip().lower()
    if forced_ua in ("clash", "meta", "stash"):
        return "clash"
    if forced_ua in ("singbox", "sing-box", "nekobox"):
        return "sing-box"
    if forced_ua in ("v2ray", "v2rayng", "raw"):
        return "v2ray"
    return user_agent


def _response_format(user_agent: str):

    if any(x in user_agent for x in ("clash", "meta", "stash")):
        return "clash", "text/yaml", "yaml"
    if any(x in user_agent for x in ("sing-box", "nekobox")):
        return "singbox", "application/json", "json"
    return "v2ray", "text/plain; charset=utf-8", "txt"


def _encode_links(links) -> str | None:

    if not links:
        return None
    return base64.b64encode("\n".join(links).encode("utf-8")).decode("utf-8")


def _gate_request_headers() -> dict:

    return {
        "x-hwid": request.headers.get("x-hwid", ""),
        "x-device-os": request.headers.get("x-device-os", ""),
        "x-ver-os": request.headers.get("x-ver-os", ""),
        "x-device-model": request.headers.get("x-device-model", ""),
        "user-agent": request.headers.get("User-Agent", ""),
        "_request_ip": (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()),
    }


def _userinfo_headers(*, up, down, total, expiry_ms, title) -> dict:

    headers = {"Profile-Update-Interval": str(_update_interval_hours())}
    expire_s = int(expiry_ms or 0) // 1000 if expiry_ms else 0
    headers["subscription-userinfo"] = (
        f"upload={int(up or 0)}; download={int(down or 0)}; total={int(total or 0)}; expire={expire_s}"
    )
    if title:
        headers["profile-title"] = title
    return headers


def _user_headers(client=None) -> dict:

    if client is None:
        return {"Profile-Update-Interval": str(_update_interval_hours())}

    return _userinfo_headers(
        up=client.up,
        down=client.down,
        total=client.limit_bytes,
        expiry_ms=client.expiry_time,
        title=client.email,
    )


def _snapshot_client_headers(client_data) -> dict:

    return _userinfo_headers(
        up=client_data.get("up", 0),
        down=client_data.get("down", 0),
        total=client_data.get("limit_bytes", 0),
        expiry_ms=client_data.get("expiry_time", 0),
        title=client_data.get("email") or "",
    )


def _aggregate_user_headers(clients) -> dict:

    headers = {"Profile-Update-Interval": str(_update_interval_hours())}

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
    configured = build_aggregate_sub_url(token)
    if configured:
        return configured
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme or "https")
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}/api/sub/u/{token}"


def sub_page_dist() -> str:
    return os.getenv("SUB_PAGE_DIST", "/app/ui")


def sub_page_index_path() -> str:
    return os.path.join(sub_page_dist(), "index.html")


_BUNDLE_MISSING = "Subscription page bundle is not installed"


@bp.route("/sub/u/assets/<path:filename>", methods=["GET"])
@limiter.limit("600 per minute")
def get_sub_page_asset(filename):
    assets_dir = os.path.join(sub_page_dist(), "assets")
    if not os.path.isdir(assets_dir):
        return _BUNDLE_MISSING, 503
    return send_from_directory(assets_dir, filename, max_age=31536000)


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
        index_path = sub_page_index_path()
        if not os.path.isfile(index_path):
            return Response(_BUNDLE_MISSING, status=503, mimetype="text/plain; charset=utf-8")
        with open(index_path, "r", encoding="utf-8") as fh:
            shell = fh.read()
        return Response(shell, mimetype="text/html; charset=utf-8", headers=headers)

    from panel_core.services.device_tracking import user_device_gate

    gate_state, extra_headers = user_device_gate(user.telegram_id, _gate_request_headers())
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
    user_agent = _resolve_user_agent()

    client = db.session.get(Client, uuid_str)
    if client and client.enable:
        inbound = Inbound.query.filter_by(tag=client.inbound_tag).first()
        if not inbound:
            return "User not found", 404
        telegram_id = client.telegram_id
        info_headers = _user_headers(client)
        email = client.email
        builders = {
            "v2ray": lambda: _encode_links(get_subscription_content(uuid_str)),
            "clash": lambda: generate_clash_config(uuid_str),
            "singbox": lambda: generate_singbox_config(uuid_str),
        }
    else:
        pair = _remote_pair_for_uuid(uuid_str)
        if pair is None:
            return "User not found", 404
        host, ib_data, client_data, stream = pair
        telegram_id = client_data.get("telegram_id")
        info_headers = _snapshot_client_headers(client_data)
        email = client_data.get("email") or ""
        builders = {
            "v2ray": lambda: _encode_links(_build_remote_link(host, ib_data, client_data)),
            "clash": lambda: _remote_clash_config(host, ib_data, client_data, stream),
            "singbox": lambda: _remote_singbox_config(host, ib_data, client_data, stream),
        }

    from panel_core.services.device_tracking import user_device_gate

    state, extra_headers = user_device_gate(telegram_id, _gate_request_headers())
    if state != "ok":
        return _warn_response(state, user_agent, extra_headers)

    kind, mimetype, ext = _response_format(user_agent)
    body = sub_cache.get(kind, uuid_str)
    if body is None:
        body = builders[kind]()
        if not body:
            return "User not found", 404
        sub_cache.set(kind, uuid_str, body)

    return Response(
        body,
        mimetype=mimetype,
        headers={
            "Content-Disposition": f'attachment; filename="{_filename_from_email(email, ext)}"',
            **info_headers,
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


def _iter_remote_pairs():

    from urllib.parse import urlparse
    from panel_core.models import LinkedPanel
    from panel_core.services.panel_proxy import get_panel_snapshot

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
                if not c.get("enable", True):
                    continue
                stream = ib_data.get("stream_settings", {})
                if isinstance(stream, str):
                    try:
                        stream = json.loads(stream)
                    except Exception:
                        stream = {}
                yield host, ib_data, c, stream


def _remote_inbound_client_pairs(telegram_id):

    if not telegram_id:
        return
    for host, ib_data, c, stream in _iter_remote_pairs():
        if c.get("telegram_id") != telegram_id:
            continue
        yield host, ib_data, c, stream


def _remote_pair_for_uuid(uuid_str):

    try:
        for host, ib_data, c, stream in _iter_remote_pairs():
            if c.get("id") == uuid_str:
                return host, ib_data, c, stream
    except Exception:
        return None
    return None


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


def _clash_document(proxies):

    if not proxies:
        return None
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
                "proxies": [p["name"] for p in proxies],
            }
        ],
        "rules": ["GEOIP,CN,DIRECT", "MATCH,FASTEST"],
    }
    return yaml.dump(config, sort_keys=False, allow_unicode=True)


def _singbox_document(outbounds):

    if not outbounds:
        return None
    config = {
        "log": {"level": "info", "timestamp": True},
        "dns": {
            "servers": [
                {"tag": "google", "type": "udp", "server": "8.8.8.8", "detour": "proxy"},
                {"tag": "local", "type": "local"},
            ]
        },
        "inbounds": [{"type": "tun", "tag": "tun-in", "address": ["172.19.0.1/30"], "auto_route": True}],
        "outbounds": outbounds + [{"type": "direct", "tag": "direct"}],
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
    return _clash_document([proxy_node])


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
    return _singbox_document([outbound])


def _remote_clash_config(host, ib_data, client_data, stream):

    label = ib_data.get("label") or ib_data.get("tag", "remote")
    proxy_node = _build_clash_proxy(
        f"{label}-{client_data.get('email') or client_data.get('id', '')}",
        ib_data.get("protocol", ""),
        host,
        ib_data.get("port", 443),
        stream,
        client_data.get("id", ""),
        client_data.get("flow", ""),
    )
    return _clash_document([proxy_node])


def _remote_singbox_config(host, ib_data, client_data, stream):

    outbound = _build_singbox_outbound(
        "proxy",
        ib_data.get("protocol", ""),
        host,
        ib_data.get("port", 443),
        stream,
        client_data.get("id", ""),
        client_data.get("flow", ""),
    )
    return _singbox_document([outbound])


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

    return _clash_document(proxies)


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

    from panel_core.services.device_tracking import count_user_devices, subscription_device_settings

    enabled, limit = subscription_device_settings()
    if not enabled:
        return None
    return {"count": count_user_devices(telegram_id), "limit": limit}


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
