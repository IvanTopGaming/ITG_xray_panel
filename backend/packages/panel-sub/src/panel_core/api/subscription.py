import base64
import hashlib
import json
import logging
import os
import time
from urllib.parse import quote
import yaml
from flask import Blueprint, jsonify, request, Response, send_from_directory
from panel_core.extensions import limiter, db
from panel_core.models import Client, Inbound, SystemSetting, TelegramUser
from panel_core.services import sub_cache
from panel_core.services.expiry import nearest_expiry
from panel_core.services.subscription_sources import (
    SubscriptionUnavailable,
    UnsupportedSubscriptionFormat,
    access_reason,
    aggregate_access_reason,
    remote_subscription_clients,
)
from panel_core.services.sub_links import build_aggregate_sub_url  # noqa: F401 — re-exported under the original name
from panel_core.xray.protocol import stream_supports_vless_flow
from panel_core.services.share_links import (
    build_remote_link,
    build_share_links,
    extract_tls_alpn,
    extract_tls_server_name,
    extract_tls_utls_fingerprint,
    extract_transport_path_host,
    is_ss2022_method,
    normalize_reality_public_key,
    normalize_ss2022_key,
)


bp = Blueprint("subscription", __name__)
logger = logging.getLogger(__name__)


@bp.errorhandler(SubscriptionUnavailable)
def subscription_unavailable(exc):
    unsupported = isinstance(exc, UnsupportedSubscriptionFormat)
    logger.warning("subscription request could not be rendered: %s", exc)
    return Response(
        "Unsupported subscription format or transport. Use the raw subscription with a compatible client."
        if unsupported
        else "Subscription data is temporarily unavailable. Please retry later.",
        status=422 if unsupported else 503,
        mimetype="text/plain",
        headers={"Cache-Control": "no-store", "Retry-After": "30"},
    )


def _get_remote_links_for_client(client_uuid: str, telegram_id: int | None) -> list[str]:
    remote_links = []
    for host, inbound, client, stream in remote_subscription_clients(
        telegram_id=telegram_id, client_uuid=None if telegram_id else client_uuid
    ):
        try:
            links = build_remote_link(host, {**inbound, "stream_settings": stream}, client)
            if not links:
                raise UnsupportedSubscriptionFormat("The active protocol has no raw subscription representation")
            remote_links.extend(links)
        except UnsupportedSubscriptionFormat:
            raise
        except Exception as exc:
            logger.exception("subscription link build failed: panel_id=%s phase=render", inbound.get("panel_id"))
            raise SubscriptionUnavailable("Subscription link cannot be generated") from exc
    return remote_links


def _remote_clients_for_headers(telegram_id, *, only_enabled=True):
    from types import SimpleNamespace

    out = []
    if not telegram_id:
        return out
    for _, _, client, _ in remote_subscription_clients(telegram_id=telegram_id, only_enabled=only_enabled):
        expiry = client.get("expiry_time")
        out.append(
            SimpleNamespace(
                up=int(client.get("up", 0) or 0),
                down=int(client.get("down", 0) or 0),
                limit_bytes=int(client.get("limit_bytes", 0) or 0),
                expiry_time=int(expiry) if expiry is not None else None,
                enable=bool(client.get("enable", True)),
            )
        )
    return out


WARN_REMARK = {
    "unsupported": "⚠ Use Happ / v2RayTun / Shadowrocket — client unsupported",
    "limit": "⚠ Device limit reached — open subscription page",
}

NO_ACCESS_REMARK = {
    "ru": "⛔ Подписка закончилась — продлите в {where}",
    "en": "⛔ Subscription ended — renew in {where}",
}


def _bot_handle() -> str:

    row = SystemSetting.query.filter_by(key="bot_username").first()
    handle = (row.value if row else "").strip().lstrip("@")
    return f"@{handle}" if handle else ""


def _no_access_remark(lang: str) -> str:
    """§109: an expired subscription used to answer 404, exactly like a link that does not exist.

    A client app renders that as a failed update, so the one screen a person looks at when the
    VPN stops working could not tell them why — and after wave 6's link reset the two cases are
    genuinely different instructions. The app will however show the *name* of a server, so the
    message travels as a single entry pointing at a dead address: nothing to connect to, nothing
    that hangs, no real credential handed out over a link that may have leaked. An unknown token
    still answers 404 on purpose, or revoking a link would look like an expiry and probing random
    tokens would get a meaningful answer.
    """

    template = NO_ACCESS_REMARK.get((lang or "ru").lower(), NO_ACCESS_REMARK["ru"])
    handle = _bot_handle()
    where = handle or ("боте" if (lang or "ru").lower() == "ru" else "the bot")
    return template.format(where=where)


def _remark_for(state: str, lang: str = "ru") -> str:
    if state in {"no_access", "expired"}:
        return _no_access_remark(lang)
    if state in {"blocked", "disabled", "not_configured", "traffic_exhausted"}:
        texts = {
            "blocked": ("⛔ Аккаунт заблокирован — обратитесь в поддержку", "⛔ Account blocked — contact support"),
            "disabled": ("⛔ Доступ отключён — обратитесь в поддержку", "⛔ Access disabled — contact support"),
            "not_configured": ("⛔ Доступ пока не выдан — откройте бота", "⛔ Access not issued yet — open the bot"),
            "traffic_exhausted": (
                "⛔ Лимит трафика исчерпан — откройте бота",
                "⛔ Traffic limit reached — open the bot",
            ),
        }
        return texts[state][1 if lang == "en" else 0]
    return WARN_REMARK[state]


def _warn_v2ray(state: str, lang: str = "ru") -> str:
    remark = quote(_remark_for(state, lang), safe="")
    link = f"vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?encryption=none#{remark}"
    return base64.b64encode(link.encode("utf-8")).decode("utf-8")


def _warn_clash(state: str, lang: str = "ru") -> str:
    name = _remark_for(state, lang)
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


def _warn_singbox(state: str, lang: str = "ru") -> str:
    name = _remark_for(state, lang)
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


def _warn_response(state: str, user_agent: str, extra_headers: dict, *, lang: str = "ru", info=None) -> Response:

    base = info if info is not None else _user_headers()
    if any(x in user_agent for x in ["clash", "meta", "stash"]):
        body = _warn_clash(state, lang)
        return Response(
            body,
            mimetype="text/yaml",
            headers={
                "Content-Disposition": 'attachment; filename="config.yaml"',
                **base,
                **extra_headers,
            },
        )
    if any(x in user_agent for x in ["sing-box", "nekobox"]):
        body = _warn_singbox(state, lang)
        return Response(
            body,
            mimetype="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="config.json"',
                **base,
                **extra_headers,
            },
        )
    body = _warn_v2ray(state, lang)
    return Response(
        body,
        mimetype="text/plain",
        headers={
            "Content-Disposition": 'attachment; filename="config.txt"',
            **base,
            **extra_headers,
        },
    )


def _filename_from_email(email, ext: str) -> str:

    if not email:
        return f"config.{ext}"
    safe = "".join(c if c.isascii() and (c.isalnum() or c in "._-") else "_" for c in str(email))
    return f"{safe.strip('._') or 'config'}.{ext}"


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
    return "v2ray", "text/plain", "txt"


def _encode_links(links) -> str | None:

    if not links:
        return None
    return base64.b64encode("\n".join(links).encode("utf-8")).decode("utf-8")


def _cache_revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _local_revision(client):
    inbound = Inbound.query.filter_by(tag=client.inbound_tag).first()
    if inbound is None:
        raise SubscriptionUnavailable("Client inbound is missing")
    return [
        client.to_dict(),
        inbound.protocol,
        inbound.port,
        inbound.stream_settings,
        inbound.label,
        os.getenv("PANEL_DOMAIN", "localhost"),
    ]


def _cached_body(kind, key, revision, builder):
    cached = sub_cache.get(kind, key)
    if cached is not None:
        try:
            envelope = json.loads(cached)
            if (
                isinstance(envelope, dict)
                and envelope.get("revision") == revision
                and isinstance(envelope.get("body"), str)
            ):
                return envelope["body"]
        except (ValueError, TypeError):
            logger.warning("subscription cache entry invalid; rebuilding kind=%s", kind)
    body = builder()
    if not body:
        raise UnsupportedSubscriptionFormat("The active protocol has no subscription representation")
    sub_cache.set(kind, key, json.dumps({"revision": revision, "body": body}))
    return body


def _gate_request_headers() -> dict:

    return {
        "x-hwid": request.headers.get("x-hwid", ""),
        "x-device-os": request.headers.get("x-device-os", ""),
        "x-ver-os": request.headers.get("x-ver-os", ""),
        "x-device-model": request.headers.get("x-device-model", ""),
        "user-agent": request.headers.get("User-Agent", ""),
        "_request_ip": request.remote_addr or "",
    }


def _userinfo_headers(*, up, down, total, expiry_ms, title) -> dict:

    headers = {"Profile-Update-Interval": str(_update_interval_hours())}
    headers["subscription-userinfo"] = f"upload={int(up or 0)}; download={int(down or 0)}; total={int(total or 0)}"
    if expiry_ms is not None:
        headers["subscription-userinfo"] += f"; expire={int(expiry_ms) // 1000}"
    if title:
        title = str(title)
        if not title.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in title):
            title = "base64:" + base64.b64encode(title.encode("utf-8")).decode("ascii")
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
        expiry_ms=client_data.get("expiry_time"),
        title=client_data.get("email") or "",
    )


def _aggregate_user_headers(clients) -> dict:

    headers = {"Profile-Update-Interval": str(_update_interval_hours())}

    brand = SystemSetting.query.filter_by(key="brand_name").first()
    title = (brand.value if brand and brand.value else "Subscription").strip()[:25]
    if title.isascii() and not any(ord(char) < 32 or ord(char) == 127 for char in title):
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

    expiry = nearest_expiry([c.expiry_time for c in clients], fallback=None)
    headers["subscription-userinfo"] = f"upload={upload}; download={download}; total={total}"
    if expiry is not None:
        headers["subscription-userinfo"] += f"; expire={expiry // 1000}"
    return headers


def _apply_clash_transport(proxy_node, stream):
    network = str(stream.get("network", "tcp") or "tcp").strip().lower()
    path, host = extract_transport_path_host(stream)

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
        settings = stream.get("xhttpSettings" if network == "xhttp" else "splitHttpSettings", {})
        if settings.get("extra"):
            raise UnsupportedSubscriptionFormat("Advanced XHTTP options need an explicit compatible export")
        proxy_node["network"] = "xhttp"
        proxy_node["xhttp-opts"] = {"path": path or "/", "mode": settings.get("mode", "auto")}
        if host:
            proxy_node["xhttp-opts"]["host"] = host
        return
    if network not in {"tcp", "http"}:
        raise UnsupportedSubscriptionFormat(f"Unsupported Clash transport: {network}")
    if network == "http":
        settings = stream.get("httpSettings", {})
        proxy_node["network"] = "h2"
        proxy_node["h2-opts"] = {"path": settings.get("path", "/"), "host": settings.get("host", [])}
        return
    proxy_node["network"] = network


def _apply_singbox_transport(outbound, stream):
    network = str(stream.get("network", "tcp") or "tcp").strip().lower()
    path, host = extract_transport_path_host(stream)

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

    if network == "http":
        settings = stream.get("httpSettings", {})
        outbound["transport"] = {
            "type": "http",
            "path": settings.get("path", "/"),
            "host": settings.get("host", []),
        }
        return
    if network != "tcp":
        raise UnsupportedSubscriptionFormat(f"Unsupported sing-box transport: {network}")


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
    if not user:
        return "User not found", 404

    user_agent = request.headers.get("User-Agent", "").lower()
    forced_ua = (request.args.get("ua", "") or "").strip().lower()
    if forced_ua in ("clash", "meta", "stash"):
        user_agent = "clash"
    elif forced_ua in ("singbox", "sing-box", "nekobox"):
        user_agent = "sing-box"
    elif forced_ua in ("v2ray", "v2rayng", "raw"):
        user_agent = "v2ray"

    clients = (
        []
        if user.blocked
        else [
            client
            for client in Client.query.filter_by(telegram_id=user.telegram_id).all()
            if access_reason(client) == "active"
        ]
    )
    if not user.blocked:
        clients = clients + _remote_clients_for_headers(user.telegram_id)
    headers = _aggregate_user_headers(clients)

    def no_access(extra):
        every = Client.query.filter_by(telegram_id=user.telegram_id).all()
        if not user.blocked:
            every = every + _remote_clients_for_headers(user.telegram_id, only_enabled=False)
        return _warn_response(
            aggregate_access_reason(every, blocked=user.blocked),
            user_agent,
            extra,
            lang=user.language or "ru",
            info=_aggregate_user_headers(every),
        )

    if _looks_like_browser(user_agent):
        index_path = sub_page_index_path()
        if not os.path.isfile(index_path):
            return Response(_BUNDLE_MISSING, status=503, mimetype="text/plain")
        with open(index_path, "r", encoding="utf-8") as fh:
            shell = fh.read()
        return Response(shell, mimetype="text/html", headers=headers)

    if user.blocked or not clients:
        return no_access({})

    from panel_core.services.device_tracking import user_device_gate

    gate_state, extra_headers = user_device_gate(user.telegram_id, _gate_request_headers())
    if gate_state != "ok":
        return _warn_response(gate_state, user_agent, extra_headers, lang=user.language or "ru")

    kind, mimetype, ext = _response_format(user_agent)
    revision = _cache_revision(
        [
            [_local_revision(client) for client in clients if isinstance(client, Client)],
            list(remote_subscription_clients(telegram_id=user.telegram_id)),
        ]
    )
    builders = {
        "v2ray": lambda: _encode_links(get_subscription_content_for_user(user.telegram_id)),
        "clash": lambda: generate_clash_config_for_user(user.telegram_id),
        "singbox": lambda: generate_singbox_config_for_user(user.telegram_id),
    }
    cached = _cached_body(f"u-{kind}", token, revision, builders[kind])
    return Response(
        cached,
        mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="config.{ext}"', **headers, **extra_headers},
    )


@bp.route("/sub/<path:uuid_str>", methods=["GET"])
@limiter.limit("180 per minute")
def get_subscription(uuid_str):
    user_agent = _resolve_user_agent()

    client = db.session.get(Client, uuid_str)
    if client:
        inbound = Inbound.query.filter_by(tag=client.inbound_tag).first()
        if not inbound:
            raise SubscriptionUnavailable("Client inbound is missing")
        telegram_id = client.telegram_id
        info_headers = _user_headers(client)
        email = client.email
        access_client = client
        revision = _cache_revision(_local_revision(client))
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
        access_client = client_data
        revision = _cache_revision(pair)
        builders = {
            "v2ray": lambda: _encode_links(build_remote_link(host, ib_data, client_data)),
            "clash": lambda: _remote_clash_config(host, ib_data, client_data, stream),
            "singbox": lambda: _remote_singbox_config(host, ib_data, client_data, stream),
        }

    owner = db.session.get(TelegramUser, telegram_id) if telegram_id else None
    reason = access_reason(access_client, blocked=bool(owner and owner.blocked))
    if reason != "active":
        return _warn_response(
            reason, user_agent, {}, lang=(owner.language if owner else "ru") or "ru", info=info_headers
        )

    from panel_core.services.device_tracking import user_device_gate

    state, extra_headers = user_device_gate(telegram_id, _gate_request_headers())
    if state != "ok":
        return _warn_response(state, user_agent, extra_headers)

    kind, mimetype, ext = _response_format(user_agent)
    body = _cached_body(kind, uuid_str, revision, builders[kind])

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
    remote = _get_remote_links_for_client(uuid_str, None)
    links = (local or []) + remote
    return links if links else None


def _enabled_client_ids_for_user(telegram_id):

    rows = Client.query.filter_by(telegram_id=telegram_id, enable=True).all()
    return [row.id for row in rows if row.id and access_reason(row) == "active"]


def get_subscription_content_for_user(telegram_id):

    links = []
    for cid in _enabled_client_ids_for_user(telegram_id):
        local = _get_local_subscription_content(cid)
        if local:
            links.extend(local)
    remote = _get_remote_links_for_client(None, telegram_id)
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
    return build_share_links(host, ib.protocol, ib.port, stream, client.id, client.flow or "", ib.label or ib.tag)


def _iter_remote_pairs():
    yield from remote_subscription_clients()


def _remote_inbound_client_pairs(telegram_id):

    if not telegram_id:
        return
    yield from remote_subscription_clients(telegram_id=telegram_id)


def _remote_pair_for_uuid(uuid_str):
    for pair in remote_subscription_clients(client_uuid=uuid_str, only_enabled=False):
        return pair
    return None


def _build_clash_proxy(name, protocol, host, port, stream, client_id, flow):

    if protocol not in {"vless", "vmess", "trojan", "shadowsocks"}:
        raise UnsupportedSubscriptionFormat(f"Unsupported Clash protocol: {protocol}")
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
            "public-key": normalize_reality_public_key(r.get("publicKey", "")),
            "short-id": (r.get("shortIds") or [""])[0],
        }

    def _tls(n):
        sni = extract_tls_server_name(stream)
        if sni:
            n["servername"] = sni
        fp = extract_tls_utls_fingerprint(stream)
        if fp:
            n["client-fingerprint"] = fp
        alpn = extract_tls_alpn(stream)
        if alpn:
            n["alpn"] = alpn

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
        if is_ss2022_method(method):
            server_pass = normalize_ss2022_key(server_pass)
            user_pass = normalize_ss2022_key(user_pass)
        node["cipher"] = method
        node["password"] = f"{server_pass}:{user_pass}" if is_ss2022_method(method) else user_pass

    _apply_clash_transport(node, stream)
    return node


def _build_singbox_outbound(tag, protocol, host, port, stream, client_id, flow):

    if protocol not in {"vless", "vmess", "trojan", "shadowsocks"}:
        raise UnsupportedSubscriptionFormat(f"Unsupported sing-box protocol: {protocol}")
    if not isinstance(stream, dict):
        stream = {}
    security = stream.get("security", "none")
    ob = {"tag": tag, "server": host, "server_port": port, "type": protocol}

    def _tls():
        p = {"enabled": True}
        sni = extract_tls_server_name(stream)
        if sni:
            p["server_name"] = sni
        alpn = extract_tls_alpn(stream)
        if alpn:
            p["alpn"] = alpn
        fp = extract_tls_utls_fingerprint(stream)
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
                "public_key": normalize_reality_public_key(r.get("publicKey", "")),
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
        if is_ss2022_method(method):
            server_pass = normalize_ss2022_key(server_pass)
            user_pass = normalize_ss2022_key(user_pass)
        ob["password"] = f"{server_pass}:{user_pass}" if is_ss2022_method(method) else user_pass

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
        name = f"{label}-{c.get('email') or c.get('id', '')} [{ib_data['panel_id']}:{ib_data.get('tag', '')}:{c.get('id', '')}]"
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
        except UnsupportedSubscriptionFormat:
            raise
        except Exception as exc:
            logger.exception("subscription Clash build failed: panel_id=%s phase=render", ib_data.get("panel_id"))
            raise SubscriptionUnavailable("Clash configuration cannot be generated") from exc

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
        tag = f"{label}-{c.get('email') or c.get('id', '')} [{ib_data['panel_id']}:{ib_data.get('tag', '')}:{c.get('id', '')}]"
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
        except UnsupportedSubscriptionFormat:
            raise
        except Exception as exc:
            logger.exception("subscription sing-box build failed: panel_id=%s phase=render", ib_data.get("panel_id"))
            raise SubscriptionUnavailable("sing-box configuration cannot be generated") from exc

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
                "expiry": c.expiry_time,
                "expiry_raw": c.expiry_time,
                "online": online,
                "enabled": access_reason(c) == "active",
                "reason": access_reason(c),
                "unlimited": limit <= 0,
            }
        )

    from panel_core.services.panel_proxy import get_panel_liveness

    for _, inbound, client, stream in remote_subscription_clients(telegram_id=telegram_id, only_enabled=False):
        live_status, _ = get_panel_liveness(inbound["panel_id"])
        used = int(client.get("up", 0) or 0) + int(client.get("down", 0) or 0)
        limit = int(client.get("limit_bytes", 0) or 0)
        enabled = access_reason(client) == "active"
        nodes.append(
            {
                "name": inbound.get("label") or inbound.get("tag", "remote"),
                "tag": _protocol_tag(inbound.get("protocol", ""), stream),
                "used": used,
                "limit": limit,
                "expiry": client.get("expiry_time"),
                "expiry_raw": client.get("expiry_time"),
                "online": live_status == "online" and enabled,
                "enabled": enabled,
                "reason": access_reason(client),
                "unlimited": limit <= 0,
            }
        )

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

    nodes = [] if user.blocked else _user_page_nodes(user.telegram_id)
    dev = _user_device_summary(user.telegram_id)

    interval_row = SystemSetting.query.filter_by(key="subscription_update_interval_hours").first()
    try:
        interval = int(interval_row.value) if interval_row and interval_row.value else 24
        if interval < 1:
            interval = 24
    except (ValueError, TypeError):
        interval = 24

    enabled_expiries = [n["expiry_raw"] for n in nodes if n["enabled"]]
    known_enabled = [e for e in enabled_expiries if e is not None]
    expiries = enabled_expiries if known_enabled else [n["expiry_raw"] for n in nodes]
    active = not user.blocked and any(n["enabled"] for n in nodes)
    reasons = {node["reason"] for node in nodes}
    reason = (
        "blocked"
        if user.blocked
        else next(
            (reason for reason in ("active", "disabled", "traffic_exhausted", "expired") if reason in reasons),
            "not_configured",
        )
    )

    return {
        "brand": brand,
        "sub_url": _absolute_sub_url(token),
        "status": "active" if active else "disabled",
        "reason": reason,
        "expiry_at": nearest_expiry(expiries, fallback=None),
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
    if not user:
        return "User not found", 404
    response = jsonify(_subscription_info_payload(user, token))
    response.headers["Cache-Control"] = "no-store"
    return response
