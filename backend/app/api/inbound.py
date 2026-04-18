import json
import uuid
import base64
import secrets
from flask import Blueprint, request, jsonify
from app.extensions import db
from app.models import Inbound, Client
from app.utils import token_required, normalize_tag, normalize_email, parse_int
from app.services.xray import (
    generate_config_file,
    restart_xray_container,
    _build_stream_settings,
    _validate_port,
    _derive_reality_pubkey,
    _derive_wg_pubkey,
    _normalize_fallback_dest,
    is_shadowsocks_2022_method,
    normalize_shadowsocks_2022_key,
    generate_shadowsocks_user_key,
)
from app.services.stats import (
    _api_add_user_grpc,
    _api_remove_user_grpc,
    reset_user_traffic,
    reset_inbound_traffic,
    bulk_delete_users,
)
from app.services import sub_cache

bp = Blueprint("inbound", __name__)
MAX_CLIENT_ID_LEN = 128
ALLOWED_INBOUND_PROTOCOLS = {
    "vless",
    "vmess",
    "trojan",
    "shadowsocks",
    "wireguard",
    "socks",
    "http",
}
PANEL_USER_PROTOCOLS = {"vless", "vmess", "trojan", "shadowsocks", "wireguard"}


def _normalize_node_groups(value):
    if value is None:
        return ""
    if isinstance(value, list):
        items = [str(g).strip() for g in value if str(g).strip()]
    else:
        items = [g.strip() for g in str(value or "").split(",") if g.strip()]
    for g in items:
        if len(g) > 30 or not all(ch.isalnum() or ch in "-_" for ch in g):
            raise ValueError("Group tags must be 1-30 chars: letters, digits, '-', '_'")
    return ",".join(sorted(set(items)))


def _parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ["1", "true", "yes", "on"]
    return bool(value)


def _normalize_client_id(value, protocol):
    client_id = str(value or "").strip()
    if not client_id:
        raise ValueError("User ID required")
    if len(client_id) > MAX_CLIENT_ID_LEN:
        raise ValueError("User ID too long")
    if protocol in ["vless", "vmess"]:
        try:
            uuid.UUID(client_id)
        except Exception:
            raise ValueError("User ID must be a valid UUID for VLESS/VMess")
    if protocol == "wireguard" and not _derive_wg_pubkey(client_id):
        raise ValueError("User ID must be a valid WireGuard private key")
    return client_id


def _normalize_inbound_protocol(value):
    protocol = str(value or "").strip().lower()
    if not protocol:
        raise ValueError("Protocol required")
    if protocol not in ALLOWED_INBOUND_PROTOCOLS:
        raise ValueError("Unsupported protocol")
    return protocol


def _normalize_fallback_address(raw_value, protocol):
    value = str(raw_value or "").strip()
    if not value:
        return None

    if protocol in ["vless", "trojan"]:
        _normalize_fallback_dest(value)

    return value


def _extract_ss_method(stream_settings_raw):
    try:
        stream = json.loads(stream_settings_raw or "{}")
        if isinstance(stream, dict):
            return str(stream.get("ssMethod", "") or "").strip().lower()
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return ""


@bp.route("/inbounds", methods=["GET"])
@token_required
def get_inbounds():
    inbounds = Inbound.query.all()
    result = []
    for ib in inbounds:
        stream = json.loads(ib.stream_settings)
        if stream.get("security") == "reality":
            reality_settings = stream.get("realitySettings", {})
            if isinstance(reality_settings, dict):
                reality_public = (reality_settings.get("publicKey") or "").strip()
                if not reality_public and reality_settings.get("privateKey"):
                    derived_public = _derive_reality_pubkey(reality_settings.get("privateKey"))
                    if derived_public:
                        reality_settings["publicKey"] = derived_public
                        stream["realitySettings"] = reality_settings
        if ib.protocol == "wireguard" and stream.get("wgSecretKey") and not stream.get("wgPublicKey"):
            derived_public = _derive_wg_pubkey(stream.get("wgSecretKey"))
            if derived_public:
                stream["wgPublicKey"] = derived_public
        clients_data = [c.to_dict() for c in ib.clients] if ib.protocol in PANEL_USER_PROTOCOLS else []
        result.append(
            {
                "tag": ib.tag,
                "port": ib.port,
                "protocol": ib.protocol,
                "streamSettings": stream,
                "settings": {"clients": clients_data},
                "routing_profile_id": ib.routing_profile_id,
                "up": ib.up,
                "down": ib.down,
                "fallback_address": ib.fallback_address,
            }
        )
    return jsonify(result)


@bp.route("/inbounds", methods=["POST"])
@token_required
def create_inbound():
    data = request.get_json(silent=True) or {}
    try:
        tag = normalize_tag(data.get("tag"))
        if tag == "api":
            raise ValueError("Tag 'api' is reserved")
        port = _validate_port(data.get("port"))

        if Inbound.query.filter((Inbound.tag == tag) | (Inbound.port == port)).first():
            raise ValueError("Tag/Port exists")

        protocol = _normalize_inbound_protocol(data.get("protocol", "vless"))
        data["protocol"] = protocol
        stream = _build_stream_settings(data)
        fallback_address = _normalize_fallback_address(data.get("fallback_address"), protocol)
        routing_profile_id = data.get("routing_profile_id")
        if routing_profile_id in ["", None]:
            routing_profile_id = None
        elif routing_profile_id is not None:
            routing_profile_id = parse_int(routing_profile_id, "routing_profile_id", min_value=1)

        new_ib = Inbound(
            tag=tag,
            port=port,
            protocol=protocol,
            stream_settings=json.dumps(stream),
            routing_profile_id=routing_profile_id,
            fallback_address=fallback_address,
        )
        db.session.add(new_ib)
        db.session.commit()

        try:
            from app.services.node_sync import sync_inbound_to_all_nodes

            sync_inbound_to_all_nodes(new_ib)
        except Exception:
            pass

        generate_config_file()
        restart_xray_container()
        return jsonify({"tag": tag, "port": port}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>", methods=["PUT"])
@token_required
def update_inbound(tag):
    try:
        ib = Inbound.query.filter_by(tag=tag).first()
        if not ib:
            return jsonify({"error": "Not found"}), 404

        data = request.get_json(silent=True) or {}
        old_protocol = ib.protocol
        if "port" in data:
            new_port = _validate_port(data["port"])
            duplicate = Inbound.query.filter(Inbound.port == new_port, Inbound.tag != ib.tag).first()
            if duplicate:
                raise ValueError("Port exists")
            ib.port = new_port
        if "protocol" in data:
            ib.protocol = _normalize_inbound_protocol(data["protocol"])
        if "fallback_address" in data:
            ib.fallback_address = data["fallback_address"]
        if "routing_profile_id" in data:
            if data["routing_profile_id"] in ["", None]:
                ib.routing_profile_id = None
            else:
                ib.routing_profile_id = parse_int(data["routing_profile_id"], "routing_profile_id", min_value=1)

        merged_stream_data = dict(data)
        current_stream = json.loads(ib.stream_settings or "{}")

        if "network" not in merged_stream_data:
            merged_stream_data["network"] = current_stream.get("network", "tcp")
        if "security" not in merged_stream_data:
            merged_stream_data["security"] = current_stream.get("security", "none")

        ws_settings = current_stream.get("wsSettings", {})
        xhttp_settings = current_stream.get("xhttpSettings", {})
        http_upgrade_settings = current_stream.get("httpUpgradeSettings", {})
        split_http_settings = current_stream.get("splitHttpSettings", {})
        grpc_settings = current_stream.get("grpcSettings", {})
        tls_settings = current_stream.get("tlsSettings", {})
        reality_settings = current_stream.get("realitySettings", {})

        if "wsPath" not in merged_stream_data:
            merged_stream_data["wsPath"] = (
                ws_settings.get("path")
                or xhttp_settings.get("path")
                or http_upgrade_settings.get("path")
                or split_http_settings.get("path")
                or "/"
            )
        if "wsHost" not in merged_stream_data:
            merged_stream_data["wsHost"] = (
                ws_settings.get("headers", {}).get("Host")
                or xhttp_settings.get("host")
                or http_upgrade_settings.get("host")
                or split_http_settings.get("host")
                or ""
            )
        if "grpcServiceName" not in merged_stream_data:
            merged_stream_data["grpcServiceName"] = grpc_settings.get("serviceName", "grpc")

        if "realityDest" not in merged_stream_data:
            merged_stream_data["realityDest"] = reality_settings.get("dest", "www.google.com:443")
        if "realitySNI" not in merged_stream_data:
            merged_stream_data["realitySNI"] = (
                reality_settings.get("serverNames", ["www.google.com"])[0]
                if reality_settings.get("serverNames")
                else "www.google.com"
            )
        if "realityPrivateKey" not in merged_stream_data:
            merged_stream_data["realityPrivateKey"] = reality_settings.get("privateKey", "")
        if "realityPublicKey" not in merged_stream_data:
            merged_stream_data["realityPublicKey"] = reality_settings.get("publicKey", "")
        if "realityShortIds" not in merged_stream_data:
            merged_stream_data["realityShortIds"] = ",".join(reality_settings.get("shortIds", []))
        if "realityFingerprint" not in merged_stream_data:
            merged_stream_data["realityFingerprint"] = reality_settings.get("fingerprint", "chrome")
        if "realitySpiderX" not in merged_stream_data:
            merged_stream_data["realitySpiderX"] = reality_settings.get("spiderX", "")
        if "tlsServerName" not in merged_stream_data:
            merged_stream_data["tlsServerName"] = tls_settings.get("serverName", "")
        if "tlsAlpn" not in merged_stream_data:
            current_tls_alpn = tls_settings.get("alpn", [])
            if isinstance(current_tls_alpn, list):
                merged_stream_data["tlsAlpn"] = ",".join(
                    [str(item).strip() for item in current_tls_alpn if str(item).strip()]
                )
            elif isinstance(current_tls_alpn, str):
                merged_stream_data["tlsAlpn"] = current_tls_alpn
            else:
                merged_stream_data["tlsAlpn"] = ""
        tls_certs = tls_settings.get("certificates", [])
        first_tls_cert = tls_certs[0] if isinstance(tls_certs, list) and tls_certs else {}
        if not isinstance(first_tls_cert, dict):
            first_tls_cert = {}
        if "tlsCertFile" not in merged_stream_data:
            merged_stream_data["tlsCertFile"] = first_tls_cert.get("certificateFile", "")
        if "tlsKeyFile" not in merged_stream_data:
            merged_stream_data["tlsKeyFile"] = first_tls_cert.get("keyFile", "")
        if "tlsUTLSFingerprint" not in merged_stream_data:
            merged_stream_data["tlsUTLSFingerprint"] = tls_settings.get("_utlsFingerprint", "")

        if "ssMethod" not in merged_stream_data:
            merged_stream_data["ssMethod"] = current_stream.get("ssMethod")
        if "ssPassword" not in merged_stream_data:
            merged_stream_data["ssPassword"] = current_stream.get("ssPassword")
        if "ssNetwork" not in merged_stream_data:
            merged_stream_data["ssNetwork"] = current_stream.get("ssNetwork", current_stream.get("network", "tcp"))
        if "wgSecretKey" not in merged_stream_data:
            merged_stream_data["wgSecretKey"] = current_stream.get("wgSecretKey", "")
        if "wgPublicKey" not in merged_stream_data:
            merged_stream_data["wgPublicKey"] = current_stream.get("wgPublicKey", "")
        if "wgMTU" not in merged_stream_data:
            merged_stream_data["wgMTU"] = current_stream.get("wgMTU", "")
        if "authUser" not in merged_stream_data:
            merged_stream_data["authUser"] = current_stream.get("authUser", "")
        if "authPass" not in merged_stream_data:
            merged_stream_data["authPass"] = current_stream.get("authPass", "")

        merged_stream_data["protocol"] = ib.protocol
        built_stream_settings = _build_stream_settings(merged_stream_data)

        if old_protocol != ib.protocol and ib.protocol in PANEL_USER_PROTOCOLS:
            normalized_client_ids = set()
            pending_updates = []
            ss_method = str(built_stream_settings.get("ssMethod", "") or "").strip().lower()
            for existing_client in ib.clients:
                candidate_id = existing_client.id
                if ib.protocol == "shadowsocks" and is_shadowsocks_2022_method(ss_method):
                    normalized_ss_client_id = normalize_shadowsocks_2022_key(candidate_id, ss_method)
                    if not normalized_ss_client_id:
                        raise ValueError(
                            "Cannot switch protocol: one or more user IDs are invalid for Shadowsocks 2022"
                        )
                    candidate_id = normalized_ss_client_id
                normalized_id = _normalize_client_id(candidate_id, ib.protocol)
                if normalized_id in normalized_client_ids:
                    raise ValueError("Cannot switch protocol: duplicate user IDs after normalization")
                normalized_client_ids.add(normalized_id)
                duplicate_client = db.session.get(Client, normalized_id)
                if duplicate_client and duplicate_client.id != existing_client.id:
                    raise ValueError("Cannot switch protocol: user ID conflict with another inbound")
                if normalized_id != existing_client.id:
                    pending_updates.append((existing_client, normalized_id))
            for existing_client, normalized_id in pending_updates:
                existing_client.id = normalized_id

        ib.stream_settings = json.dumps(built_stream_settings)
        ib.fallback_address = _normalize_fallback_address(ib.fallback_address, ib.protocol)
        if ib.protocol not in PANEL_USER_PROTOCOLS:
            Client.query.filter_by(inbound_tag=ib.tag).delete()

        db.session.commit()

        try:
            from app.services.node_sync import sync_inbound_to_all_nodes

            sync_inbound_to_all_nodes(ib)
        except Exception:
            pass

        try:
            sub_cache.invalidate_all_for_inbound(ib.tag)
        except Exception:
            pass

        generate_config_file()
        restart_xray_container()
        return jsonify({"status": "updated"}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>", methods=["DELETE"])
@token_required
def delete_inbound(tag):
    try:
        ib = Inbound.query.filter_by(tag=tag).first()
        if not ib:
            return jsonify({"error": "Not found"}), 404
        deleted_tag = ib.tag
        try:
            sub_cache.invalidate_all_for_inbound(deleted_tag)
        except Exception:
            pass
        db.session.delete(ib)
        db.session.commit()

        try:
            from app.services.node_sync import sync_inbound_delete_to_all_nodes

            sync_inbound_delete_to_all_nodes(deleted_tag)
        except Exception:
            pass

        generate_config_file()
        restart_xray_container()
        return jsonify({"status": "deleted"}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>/reset-traffic", methods=["POST"])
@token_required
def reset_ib_traffic(tag):
    try:
        reset_inbound_traffic(tag)
        return jsonify({"status": "reset"}), 200
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>/users", methods=["POST"])
@token_required
def add_user(tag):
    try:
        data = request.get_json(silent=True) or {}
        ib = Inbound.query.filter_by(tag=tag).first()
        if not ib:
            return jsonify({"error": "Inbound not found"}), 404
        if ib.protocol not in PANEL_USER_PROTOCOLS:
            raise ValueError(
                "This inbound does not support panel users. Configure username/password in inbound settings."
            )
        email = normalize_email(data.get("email"))
        if Client.query.filter_by(inbound_tag=tag, email=email).first():
            raise ValueError("Email exists")

        ss_method = _extract_ss_method(ib.stream_settings)
        is_ss_2022 = ib.protocol == "shadowsocks" and is_shadowsocks_2022_method(ss_method)

        provided_id = data.get("id")
        if not provided_id:
            if ib.protocol == "wireguard":
                provided_id = base64.b64encode(secrets.token_bytes(32)).decode("utf-8")
            elif is_ss_2022:
                provided_id = generate_shadowsocks_user_key(ss_method)
            else:
                provided_id = str(uuid.uuid4())
        provided_id = _normalize_client_id(provided_id, ib.protocol)
        if is_ss_2022:
            normalized_ss_client_id = normalize_shadowsocks_2022_key(provided_id, ss_method)
            if not normalized_ss_client_id:
                raise ValueError("User ID must be a valid Shadowsocks 2022 key for selected method")
            provided_id = normalized_ss_client_id
        if db.session.get(Client, provided_id):
            raise ValueError("User ID exists")

        new_client = Client(
            id=provided_id,
            email=email,
            inbound_tag=tag,
            limit_bytes=parse_int(data.get("limit_bytes"), "limit_bytes", min_value=0),
            expiry_time=parse_int(data.get("expiry_time"), "expiry_time", min_value=0),
            enable=_parse_bool(data.get("enable"), default=True),
            reset_day=parse_int(data.get("reset_day"), "reset_day", min_value=0, max_value=31),
            flow=data.get("flow", "xtls-rprx-vision" if ib.protocol == "vless" else ""),
            global_limit_bytes=parse_int(data.get("global_limit_bytes", 0), "global_limit_bytes", min_value=0),
            allowed_node_groups=_normalize_node_groups(data.get("allowed_node_groups", "")),
        )
        db.session.add(new_client)
        db.session.commit()

        try:
            from app.services.node_sync import sync_user_create

            sync_user_create(
                email,
                {
                    "email": email,
                    "id": provided_id,
                    "limit_bytes": new_client.limit_bytes,
                    "expiry_time": new_client.expiry_time,
                    "enable": new_client.enable,
                    "reset_day": new_client.reset_day or 0,
                    "flow": new_client.flow or "",
                },
            )
        except Exception:
            pass

        try:
            sub_cache.invalidate_user(provided_id)
        except Exception:
            pass

        generate_config_file()

        if ib.protocol in ["vless", "vmess"]:
            grpc_added = _api_add_user_grpc(tag, new_client)
            if not grpc_added:
                restart_xray_container()
        else:
            restart_xray_container()

        return jsonify(new_client.to_dict()), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>/users", methods=["PUT"])
@token_required
def update_user(tag):
    try:
        data = request.get_json(silent=True) or {}
        ib = Inbound.query.filter_by(tag=tag).first()
        if not ib:
            return jsonify({"error": "Inbound not found"}), 404
        if ib.protocol not in PANEL_USER_PROTOCOLS:
            raise ValueError(
                "This inbound does not support panel users. Configure username/password in inbound settings."
            )

        old_email = normalize_email(data.get("old_email"), "Old email")
        client = Client.query.filter_by(inbound_tag=tag, email=old_email).first()
        if not client:
            return jsonify({"error": "User not found"}), 404

        new_email = normalize_email(data.get("new_email", client.email), "New email")
        if new_email != old_email and Client.query.filter_by(inbound_tag=tag, email=new_email).first():
            raise ValueError("Email exists")

        ss_method = _extract_ss_method(ib.stream_settings)
        is_ss_2022 = ib.protocol == "shadowsocks" and is_shadowsocks_2022_method(ss_method)

        requested_new_id = data.get("new_id", client.id)
        new_id = _normalize_client_id(requested_new_id, ib.protocol)
        if is_ss_2022 and str(requested_new_id).strip() != str(client.id).strip():
            normalized_ss_client_id = normalize_shadowsocks_2022_key(new_id, ss_method)
            if not normalized_ss_client_id:
                raise ValueError("User ID must be a valid Shadowsocks 2022 key for selected method")
            new_id = normalized_ss_client_id
        if new_id != client.id and db.session.get(Client, new_id):
            raise ValueError("User ID exists")
        limit_bytes = parse_int(data.get("limit_bytes", client.limit_bytes), "limit_bytes", min_value=0)
        expiry_time = parse_int(data.get("expiry_time", client.expiry_time), "expiry_time", min_value=0)
        reset_day = parse_int(data.get("reset_day", client.reset_day), "reset_day", min_value=0, max_value=31)
        enable = _parse_bool(data.get("enable"), default=client.enable)
        flow = str(data.get("flow", client.flow or "") or "").strip()
        if ib.protocol != "vless":
            flow = ""
        global_limit_bytes = parse_int(
            data.get("global_limit_bytes", client.global_limit_bytes or 0),
            "global_limit_bytes",
            min_value=0,
        )
        if "allowed_node_groups" in data:
            allowed_groups = _normalize_node_groups(data.get("allowed_node_groups"))
        else:
            allowed_groups = client.allowed_node_groups or ""

        old_runtime_email = client.email
        old_runtime_enabled = bool(client.enable)
        old_client_id = client.id

        client.email = new_email
        client.id = new_id
        client.limit_bytes = limit_bytes
        client.expiry_time = expiry_time
        client.reset_day = reset_day
        client.enable = enable
        client.flow = flow
        client.global_limit_bytes = global_limit_bytes
        client.allowed_node_groups = allowed_groups

        db.session.commit()

        try:
            from app.services.node_sync import sync_user_update

            sync_user_update(
                old_email,
                {
                    "old_email": old_email,
                    "new_email": new_email,
                    "new_id": new_id,
                    "limit_bytes": limit_bytes,
                    "expiry_time": expiry_time,
                    "enable": enable,
                    "reset_day": reset_day,
                    "flow": flow,
                },
            )
        except Exception:
            pass

        try:
            sub_cache.invalidate_user(old_client_id)
            if new_id != old_client_id:
                sub_cache.invalidate_user(new_id)
        except Exception:
            pass

        generate_config_file()

        if ib.protocol in ["vless", "vmess"]:
            grpc_failed = False
            if old_runtime_enabled:
                removed = _api_remove_user_grpc(tag, old_runtime_email)
                if not removed:
                    grpc_failed = True
            if client.enable:
                added = _api_add_user_grpc(tag, client)
                if not added:
                    grpc_failed = True
            if grpc_failed:
                restart_xray_container()
        else:
            restart_xray_container()

        return jsonify(client.to_dict()), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>/users", methods=["DELETE"])
@token_required
def delete_user_route(tag):
    try:
        ib = Inbound.query.filter_by(tag=tag).first()
        if ib and ib.protocol not in PANEL_USER_PROTOCOLS:
            raise ValueError(
                "This inbound does not support panel users. Configure username/password in inbound settings."
            )
        email = normalize_email(request.args.get("email"))
        client = Client.query.filter_by(inbound_tag=tag, email=email).first()
        if not client:
            return jsonify({"error": "User not found"}), 404
        was_enabled = bool(client.enable)
        deleted_client_id = client.id
        db.session.delete(client)
        db.session.commit()

        try:
            from app.services.node_sync import sync_user_delete

            sync_user_delete(email)
        except Exception:
            pass

        try:
            sub_cache.invalidate_user(deleted_client_id)
        except Exception:
            pass

        generate_config_file()
        if ib and ib.protocol in ["vless", "vmess"] and was_enabled:
            grpc_removed = _api_remove_user_grpc(tag, email)
            if not grpc_removed:
                restart_xray_container()
        else:
            restart_xray_container()
        return jsonify({"status": "deleted"}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/users/bulk-delete", methods=["POST"])
@token_required
def bulk_delete_users_route():
    try:
        data = request.get_json(silent=True) or {}
        users = data.get("users")
        if not isinstance(users, list) or not users:
            raise ValueError("users array required")

        normalized = []
        for user in users:
            if not isinstance(user, dict):
                raise ValueError("each user must be an object")
            normalized.append(
                {
                    "tag": normalize_tag(user.get("tag")),
                    "email": normalize_email(user.get("email")),
                }
            )

        # Capture client ids BEFORE delegating to bulk_delete_users so we can
        # invalidate their cached subscriptions afterwards.
        doomed_ids = []
        for u in normalized:
            row = Client.query.with_entities(Client.id).filter_by(inbound_tag=u["tag"], email=u["email"]).first()
            if row and row[0]:
                doomed_ids.append(row[0])

        deleted_count = bulk_delete_users(normalized)

        try:
            from app.services.node_sync import sync_user_delete

            for user in normalized:
                sync_user_delete(user["email"])
        except Exception:
            pass

        try:
            for cid in doomed_ids:
                sub_cache.invalidate_user(cid)
        except Exception:
            pass

        return jsonify({"status": "deleted", "count": deleted_count}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/users/reset-traffic", methods=["POST"])
@token_required
def reset_user_traffic_route():
    try:
        data = request.get_json(silent=True) or {}
        if "users" in data:
            if not isinstance(data["users"], list):
                raise ValueError("users must be an array")
            for u in data["users"]:
                if not isinstance(u, dict):
                    raise ValueError("each user must be an object")
                reset_user_traffic(normalize_tag(u.get("tag")), normalize_email(u.get("email")))
        else:
            reset_user_traffic(normalize_tag(data.get("tag")), normalize_email(data.get("email")))
        return jsonify({"status": "reset"}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/users/bulk-enable", methods=["POST"])
@token_required
def bulk_enable_users_route():
    try:
        data = request.get_json(silent=True) or {}
        users = data.get("users")
        if not isinstance(users, list) or not users:
            raise ValueError("users array required")
        if "enable" not in data:
            raise ValueError("enable field required")
        enable = _parse_bool(data["enable"])

        normalized = []
        for user in users:
            if not isinstance(user, dict):
                raise ValueError("each user must be an object")
            normalized.append(
                {
                    "tag": normalize_tag(user.get("tag")),
                    "email": normalize_email(user.get("email")),
                }
            )

        updated = []
        for u in normalized:
            client = Client.query.filter_by(inbound_tag=u["tag"], email=u["email"]).first()
            if not client:
                continue
            if client.enable == enable:
                continue
            ib = Inbound.query.filter_by(tag=u["tag"]).first()
            updated.append(
                {
                    "client": client,
                    "inbound": ib,
                    "was_enabled": bool(client.enable),
                }
            )
            client.enable = enable

        if not updated:
            return jsonify({"status": "ok", "count": 0}), 200

        db.session.commit()

        for item in updated:
            client = item["client"]
            try:
                from app.services.node_sync import sync_user_update

                sync_user_update(
                    client.email,
                    {
                        "old_email": client.email,
                        "new_email": client.email,
                        "new_id": client.id,
                        "limit_bytes": client.limit_bytes,
                        "expiry_time": client.expiry_time,
                        "enable": enable,
                        "reset_day": client.reset_day,
                        "flow": client.flow or "",
                    },
                )
            except Exception:
                pass
            try:
                sub_cache.invalidate_user(client.id)
            except Exception:
                pass

        generate_config_file()

        grpc_failed = False
        for item in updated:
            client = item["client"]
            ib = item["inbound"]
            if ib and ib.protocol in ["vless", "vmess"]:
                if item["was_enabled"]:
                    if not _api_remove_user_grpc(ib.tag, client.email):
                        grpc_failed = True
                if enable:
                    if not _api_add_user_grpc(ib.tag, client):
                        grpc_failed = True

        needs_restart = grpc_failed or any(
            item["inbound"] and item["inbound"].protocol not in ["vless", "vmess"] for item in updated
        )
        if needs_restart:
            restart_xray_container()

        return jsonify({"status": "ok", "count": len(updated)}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/users/bulk-groups", methods=["POST"])
@token_required
def bulk_update_groups_route():
    try:
        data = request.get_json(silent=True) or {}
        users = data.get("users")
        if not isinstance(users, list) or not users:
            raise ValueError("users array required")
        if "allowed_node_groups" not in data:
            raise ValueError("allowed_node_groups field required")
        groups = _normalize_node_groups(data["allowed_node_groups"])

        normalized = []
        for user in users:
            if not isinstance(user, dict):
                raise ValueError("each user must be an object")
            normalized.append(
                {
                    "tag": normalize_tag(user.get("tag")),
                    "email": normalize_email(user.get("email")),
                }
            )

        count = 0
        for u in normalized:
            client = Client.query.filter_by(inbound_tag=u["tag"], email=u["email"]).first()
            if not client:
                continue
            if client.allowed_node_groups == groups:
                continue
            client.allowed_node_groups = groups
            count += 1

        if count:
            db.session.commit()

        return jsonify({"status": "ok", "count": count}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500
