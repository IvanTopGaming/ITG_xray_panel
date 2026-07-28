import base64
import binascii
import json
from urllib.parse import quote, urlencode

from panel_core.xray.protocol import stream_supports_vless_flow


SS2022_METHODS = {
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
}


def normalize_reality_public_key(public_key):
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


def is_ss2022_method(method):
    return str(method or "").strip().lower() in SS2022_METHODS


def normalize_ss2022_key(value):
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


def extract_tls_server_name(stream):
    tls_settings = stream.get("tlsSettings", {})
    if isinstance(tls_settings, dict):
        name = str(tls_settings.get("serverName", "") or "").strip()
        if name:
            return name

    ws_headers = stream.get("wsSettings", {}).get("headers", {})
    if isinstance(ws_headers, dict):
        return str(ws_headers.get("Host", "") or "").strip()
    return ""


def extract_tls_alpn(stream):
    tls_settings = stream.get("tlsSettings", {})
    if not isinstance(tls_settings, dict):
        return []

    raw = tls_settings.get("alpn", [])
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def extract_tls_utls_fingerprint(stream):
    tls_settings = stream.get("tlsSettings", {})
    if not isinstance(tls_settings, dict):
        return ""
    return str(tls_settings.get("_utlsFingerprint", "") or "").strip()


def extract_transport_path_host(stream):

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


def build_share_links(host, protocol, port, stream, client_id, flow, label) -> list[str]:

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
            t_path, t_host = extract_transport_path_host(stream)
            if t_path:
                query["path"] = t_path
            if t_host:
                query["host"] = t_host

    def _add_reality(query):
        rs = stream.get("realitySettings", {}) or {}
        query["pbk"] = normalize_reality_public_key(rs.get("publicKey", ""))
        query["fp"] = rs.get("fingerprint", "chrome")
        query["sni"] = (rs.get("serverNames") or ["google.com"])[0]
        query["sid"] = (rs.get("shortIds") or [""])[0]
        spx = rs.get("spiderX", "")
        if spx:
            query["spx"] = spx

    def _add_tls(query):
        sni = extract_tls_server_name(stream)
        if sni:
            query["sni"] = sni
        alpn = extract_tls_alpn(stream)
        if alpn:
            query["alpn"] = ",".join(alpn)
        fp = extract_tls_utls_fingerprint(stream)
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
            v_path, v_host = extract_transport_path_host(stream)
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
            sni = extract_tls_server_name(stream)
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
        if is_ss2022_method(method):
            server_pass = normalize_ss2022_key(server_pass)
            user_pass = normalize_ss2022_key(user_pass)
        user_part = f"{method}:{server_pass}:{user_pass}" if is_ss2022_method(method) else f"{method}:{user_pass}"
        return [f"ss://{base64.b64encode(user_part.encode()).decode()}@{host}:{port}#{remark}"]

    return []


def build_remote_link(host: str, ib_data: dict, client_data: dict) -> list[str]:

    stream = ib_data.get("stream_settings", {})
    if isinstance(stream, str):
        try:
            stream = json.loads(stream)
        except Exception:
            stream = {}
    return build_share_links(
        host,
        ib_data.get("protocol", ""),
        ib_data.get("port", 443),
        stream,
        client_data.get("id", ""),
        client_data.get("flow", ""),
        ib_data.get("label") or ib_data.get("tag", "remote"),
    )
