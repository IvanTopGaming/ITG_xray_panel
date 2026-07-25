import json
import uuid
import base64
import secrets
from datetime import datetime
from flask import Blueprint, request, jsonify
from panel_core.extensions import db, limiter
from panel_core.models import Inbound, Client, ClientDevice, TelegramUser, TariffItem
from panel_core.api.subscription import build_aggregate_sub_url
from panel_core.utils import (
    token_required,
    admin_or_bot_token_required,
    admin_or_federation_token_required,
    normalize_tag,
    normalize_email,
    parse_int,
)
from panel_core.xray.engine import (
    generate_config_file,
    restart_xray_container,
    _normalize_fallback_dest,
)
from panel_core.xray.protocol import (
    _build_stream_settings,
    _validate_port,
    _derive_reality_pubkey,
    _derive_wg_pubkey,
    is_shadowsocks_2022_method,
    normalize_shadowsocks_2022_key,
    generate_shadowsocks_user_key,
    stream_supports_vless_flow,
    inbound_supports_vless_flow,
)
from panel_core.services.stats import (
    _api_add_user_grpc,
    _api_remove_user_grpc,
    reset_user_traffic,
    reset_inbound_traffic,
    bulk_delete_users,
)
from panel_core.services import sub_cache
from panel_core.services.tariffs import purge_tariff_items

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
ALLOWED_VLESS_FLOWS = {"", "xtls-rprx-vision"}


def _parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ["1", "true", "yes", "on"]
    return bool(value)


def _parse_optional_int(value, field):

    if value is None or value == "":
        return None
    return parse_int(value, field, min_value=0)


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
@admin_or_bot_token_required
def get_inbounds():
    from sqlalchemy import func

    inbounds = Inbound.query.all()

    counts = dict(
        db.session.query(ClientDevice.client_id, func.count(ClientDevice.id)).group_by(ClientDevice.client_id).all()
    )

    _tok_map = dict(
        db.session.query(TelegramUser.telegram_id, TelegramUser.sub_token)
        .filter(TelegramUser.sub_token.isnot(None))
        .all()
    )

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
        if ib.protocol in PANEL_USER_PROTOCOLS:
            clients_data = []
            for c in ib.clients:
                d = c.to_dict()
                d["device_count"] = int(counts.get(c.id, 0))
                d["sub_url"] = build_aggregate_sub_url(_tok_map.get(c.telegram_id)) if c.telegram_id else None
                clients_data.append(d)
        else:
            clients_data = []
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
                "device_limit": ib.device_limit,
                "label": ib.label,
                "panel_id": None,
                "panel_name": "Master",
            }
        )
    panel_filter = request.args.get("panel")
    if panel_filter != "local":
        from panel_core.models import LinkedPanel
        from panel_core.services.panel_proxy import get_panel_snapshot

        if panel_filter and panel_filter not in ("all", "local"):
            try:
                panels = [db.session.get(LinkedPanel, int(panel_filter))]
                panels = [p for p in panels if p and p.enable]
            except (ValueError, TypeError):
                panels = []
        else:
            panels = LinkedPanel.query.filter_by(enable=True).all()

        for panel in panels:
            snapshot = get_panel_snapshot(panel.id)
            if snapshot is None:
                continue
            for ib_data in snapshot.get("inbounds", []):
                ib_data["panel_id"] = panel.id
                ib_data["panel_name"] = panel.name
                if "clients" in ib_data:
                    ib_data["settings"] = {"clients": ib_data.pop("clients")}
                if "stream_settings" in ib_data and "streamSettings" not in ib_data:
                    ib_data["streamSettings"] = ib_data.pop("stream_settings")
                result.append(ib_data)

    return jsonify(result)


@bp.route("/inbounds", methods=["POST"])
@admin_or_federation_token_required
@limiter.limit("30 per minute")
def create_inbound():
    panel_id = request.args.get("panel_id", type=int)
    if panel_id:
        from panel_core.services.panel_proxy import proxy_create_inbound

        try:
            return jsonify(proxy_create_inbound(panel_id, request.get_json(silent=True) or {}))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("proxy_create_inbound failed: %s", exc)
            return jsonify({"error": f"Remote panel error: {exc}"}), 502

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
        device_limit = parse_int(data.get("device_limit", 0), "device_limit", min_value=0)
        label = (data.get("label") or "").strip() or None

        new_ib = Inbound(
            tag=tag,
            port=port,
            protocol=protocol,
            stream_settings=json.dumps(stream),
            routing_profile_id=routing_profile_id,
            fallback_address=fallback_address,
            device_limit=device_limit,
            label=label,
        )
        db.session.add(new_ib)

        generate_config_file()
        db.session.commit()
        restart_xray_container()
        return jsonify({"tag": tag, "port": port}), 201
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>", methods=["PUT"])
@admin_or_federation_token_required
@limiter.limit("30 per minute")
def update_inbound(tag):
    panel_id = request.args.get("panel_id", type=int)
    if panel_id:
        from panel_core.services.panel_proxy import proxy_update_inbound

        try:
            return jsonify(proxy_update_inbound(panel_id, tag, request.get_json(silent=True) or {}))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception:
            return jsonify({"error": "Remote panel error"}), 502

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
        if "device_limit" in data:
            ib.device_limit = parse_int(data["device_limit"], "device_limit", min_value=0)
        if "label" in data:
            label_value = (data["label"] or "").strip() or None
            ib.label = label_value

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
                elif ib.protocol in ("trojan", "shadowsocks"):
                    candidate_id = secrets.token_urlsafe(16)
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

        if ib.protocol in PANEL_USER_PROTOCOLS and not (
            ib.protocol == "vless" and stream_supports_vless_flow(built_stream_settings)
        ):
            for c in ib.clients:
                if c.flow:
                    c.flow = ""

        generate_config_file()
        db.session.commit()

        try:
            sub_cache.invalidate_all_for_inbound(ib.tag)
            for (tg_id,) in (
                Client.query.filter_by(inbound_tag=ib.tag).with_entities(Client.telegram_id).distinct().all()
            ):
                if tg_id:
                    sub_cache.invalidate_user_aggregate(tg_id)
        except Exception:
            pass

        restart_xray_container()
        return jsonify({"status": "updated"}), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>", methods=["DELETE"])
@admin_or_federation_token_required
@limiter.limit("30 per minute")
def delete_inbound(tag):
    panel_id = request.args.get("panel_id", type=int)
    if panel_id:
        from panel_core.services.panel_proxy import proxy_delete_inbound

        try:
            result = proxy_delete_inbound(panel_id, tag)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception:
            return jsonify({"error": "Remote panel error"}), 502
        purge = purge_tariff_items(TariffItem.panel_id == panel_id, TariffItem.inbound_tag == tag)
        db.session.commit()
        if isinstance(result, dict):
            result["removed_tariff_items"] = purge["removed"]
            result["disabled_tariffs"] = purge["disabled_tariffs"]
        return jsonify(result)

    try:
        ib = Inbound.query.filter_by(tag=tag).first()
        if not ib:
            return jsonify({"error": "Not found"}), 404
        deleted_tag = ib.tag
        try:
            sub_cache.invalidate_all_for_inbound(deleted_tag)
        except Exception:
            pass
        purge = purge_tariff_items(TariffItem.inbound_tag == deleted_tag, TariffItem.panel_id.is_(None))
        db.session.delete(ib)

        generate_config_file()
        db.session.commit()
        restart_xray_container()
        return (
            jsonify(
                {
                    "status": "deleted",
                    "removed_tariff_items": purge["removed"],
                    "disabled_tariffs": purge["disabled_tariffs"],
                }
            ),
            200,
        )
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>/reset-traffic", methods=["POST"])
@token_required
@limiter.limit("30 per minute")
def reset_ib_traffic(tag):
    try:
        reset_inbound_traffic(tag)
        return jsonify({"status": "reset"}), 200
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>/users", methods=["POST"])
@admin_or_federation_token_required
@limiter.limit("60 per minute")
def add_user(tag):
    panel_id = request.args.get("panel_id", type=int)
    if panel_id:
        from panel_core.services.panel_proxy import proxy_create_user

        try:
            return jsonify(proxy_create_user(panel_id, tag, request.get_json(silent=True) or {}))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception:
            return jsonify({"error": "Remote panel error"}), 502

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
            flow=(str(data.get("flow", "xtls-rprx-vision") or "").strip() if inbound_supports_vless_flow(ib) else ""),
            device_limit=_parse_optional_int(data.get("device_limit", None), "device_limit"),
        )
        db.session.add(new_client)

        generate_config_file()
        db.session.commit()

        try:
            sub_cache.invalidate_user(provided_id)
        except Exception:
            pass

        if ib.protocol in ["vless", "vmess"]:
            grpc_added = _api_add_user_grpc(tag, new_client)
            if not grpc_added:
                restart_xray_container()
        else:
            restart_xray_container()

        return jsonify(new_client.to_dict()), 201
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>/users", methods=["PUT"])
@admin_or_federation_token_required
@limiter.limit("60 per minute")
def update_user(tag):
    panel_id = request.args.get("panel_id", type=int)
    if panel_id:
        from panel_core.services.panel_proxy import proxy_update_user

        try:
            return jsonify(proxy_update_user(panel_id, tag, request.get_json(silent=True) or {}))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception:
            return jsonify({"error": "Remote panel error"}), 502

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
        if not inbound_supports_vless_flow(ib):
            flow = ""

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
        if "device_limit" in data:
            client.device_limit = _parse_optional_int(data.get("device_limit"), "device_limit")

        generate_config_file()
        db.session.commit()

        try:
            sub_cache.invalidate_user(old_client_id)
            if new_id != old_client_id:
                sub_cache.invalidate_user(new_id)
        except Exception:
            pass

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
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/inbounds/<tag>/users", methods=["DELETE"])
@admin_or_federation_token_required
@limiter.limit("60 per minute")
def delete_user_route(tag):
    panel_id = request.args.get("panel_id", type=int)
    if panel_id:
        from panel_core.services.panel_proxy import proxy_delete_user

        email = request.args.get("email", "")
        try:
            return jsonify(proxy_delete_user(panel_id, tag, email))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception:
            return jsonify({"error": "Remote panel error"}), 502

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

        generate_config_file()
        db.session.commit()

        try:
            sub_cache.invalidate_user(deleted_client_id)
        except Exception:
            pass

        if ib and ib.protocol in ["vless", "vmess"] and was_enabled:
            grpc_removed = _api_remove_user_grpc(tag, email)
            if not grpc_removed:
                restart_xray_container()
        else:
            restart_xray_container()
        return jsonify({"status": "deleted"}), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


def _split_users_by_panel(users):

    if not isinstance(users, list) or not users:
        raise ValueError("users array required")
    local = []
    remote = {}
    for user in users:
        if not isinstance(user, dict):
            raise ValueError("each user must be an object")
        entry = {
            "tag": normalize_tag(user.get("tag")),
            "email": normalize_email(user.get("email")),
        }
        pid = user.get("panel_id")
        if pid in (None, "", 0, "0"):
            local.append(entry)
        else:
            remote.setdefault(int(pid), []).append(entry)
    return local, remote


@bp.route("/users/bulk-delete", methods=["POST"])
@admin_or_federation_token_required
@limiter.limit("60 per minute")
def bulk_delete_users_route():
    try:
        from panel_core.services.panel_proxy import proxy_bulk_delete_users

        data = request.get_json(silent=True) or {}
        local, remote = _split_users_by_panel(data.get("users"))

        deleted_count = 0
        errors = []

        for panel_id, group in remote.items():
            try:
                res = proxy_bulk_delete_users(panel_id, group)
                deleted_count += int(res.get("count", 0) or 0)
            except Exception as exc:
                errors.append(str(exc))

        if local:
            doomed_ids = []
            for u in local:
                row = Client.query.with_entities(Client.id).filter_by(inbound_tag=u["tag"], email=u["email"]).first()
                if row and row[0]:
                    doomed_ids.append(row[0])

            deleted_count += bulk_delete_users(local)

            try:
                for cid in doomed_ids:
                    sub_cache.invalidate_user(cid)
            except Exception:
                pass

        resp = {"status": "deleted", "count": deleted_count}
        if errors:
            resp["errors"] = errors
        return jsonify(resp), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/users/reset-traffic", methods=["POST"])
@admin_or_federation_token_required
@limiter.limit("60 per minute")
def reset_user_traffic_route():
    try:
        from panel_core.services.panel_proxy import proxy_bulk_reset_traffic

        data = request.get_json(silent=True) or {}
        if "users" in data:
            local, remote = _split_users_by_panel(data["users"])
            for panel_id, group in remote.items():
                proxy_bulk_reset_traffic(panel_id, group)
            for u in local:
                reset_user_traffic(u["tag"], u["email"])
        else:
            panel_id = request.args.get("panel_id", type=int)
            tag = normalize_tag(data.get("tag"))
            email = normalize_email(data.get("email"))
            if panel_id:
                proxy_bulk_reset_traffic(panel_id, [{"tag": tag, "email": email}])
            else:
                reset_user_traffic(tag, email)
        return jsonify({"status": "reset"}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/users/bulk-enable", methods=["POST"])
@admin_or_federation_token_required
@limiter.limit("60 per minute")
def bulk_enable_users_route():
    try:
        from panel_core.services.panel_proxy import proxy_bulk_enable_users

        data = request.get_json(silent=True) or {}
        if "enable" not in data:
            raise ValueError("enable field required")
        enable = _parse_bool(data["enable"])
        local, remote = _split_users_by_panel(data.get("users"))

        count = 0
        errors = []
        for panel_id, group in remote.items():
            try:
                res = proxy_bulk_enable_users(panel_id, group, enable)
                count += int(res.get("count", 0) or 0)
            except Exception as exc:
                errors.append(str(exc))

        updated = []
        for u in local:
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
            resp = {"status": "ok", "count": count}
            if errors:
                resp["errors"] = errors
            return jsonify(resp), 200

        generate_config_file()
        db.session.commit()

        for item in updated:
            client = item["client"]
            try:
                sub_cache.invalidate_user(client.id)
            except Exception:
                pass

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

        count += len(updated)
        resp = {"status": "ok", "count": count}
        if errors:
            resp["errors"] = errors
        return jsonify(resp), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/users/bulk-adjust-days", methods=["POST"])
@admin_or_federation_token_required
@limiter.limit("60 per minute")
def bulk_adjust_days_route():
    try:
        from panel_core.services.panel_proxy import proxy_bulk_adjust_days

        data = request.get_json(silent=True) or {}
        days = parse_int(data.get("days"), "days", min_value=1)
        mode = str(data.get("mode") or "add").strip().lower()
        if mode not in ("add", "subtract"):
            raise ValueError("mode must be 'add' or 'subtract'")
        local, remote = _split_users_by_panel(data.get("users"))

        updated = 0
        skipped = 0
        errors = []
        for panel_id, group in remote.items():
            try:
                res = proxy_bulk_adjust_days(panel_id, group, days, mode)
                updated += int(res.get("updated", 0) or 0)
                skipped += int(res.get("skipped", 0) or 0)
            except Exception as exc:
                errors.append(str(exc))

        now_ms = int(datetime.now().timestamp() * 1000)
        delta_ms = days * 86_400_000
        updated_clients = []

        for u in local:
            client = Client.query.filter_by(inbound_tag=u["tag"], email=u["email"]).first()
            if not client or client.expiry_time == 0:
                skipped += 1
                continue
            base = max(now_ms, client.expiry_time)
            if mode == "add":
                client.expiry_time = base + delta_ms
            else:
                client.expiry_time = base - delta_ms
            updated_clients.append(client)

        if updated_clients:
            generate_config_file()
            db.session.commit()

            for client in updated_clients:
                try:
                    sub_cache.invalidate_user(client.id)
                except Exception:
                    pass

        updated += len(updated_clients)
        resp = {"status": "ok", "updated": updated, "skipped": skipped}
        if errors:
            resp["errors"] = errors
        return jsonify(resp), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/users/bulk-adjust-traffic", methods=["POST"])
@admin_or_federation_token_required
@limiter.limit("60 per minute")
def bulk_adjust_traffic_route():
    try:
        from panel_core.services.panel_proxy import proxy_bulk_adjust_traffic

        data = request.get_json(silent=True) or {}
        gb = parse_int(data.get("gb"), "gb", min_value=1)
        mode = str(data.get("mode") or "add").strip().lower()
        if mode not in ("add", "subtract"):
            raise ValueError("mode must be 'add' or 'subtract'")
        local, remote = _split_users_by_panel(data.get("users"))

        updated = 0
        skipped = 0
        errors = []
        for panel_id, group in remote.items():
            try:
                res = proxy_bulk_adjust_traffic(panel_id, group, gb, mode)
                updated += int(res.get("updated", 0) or 0)
                skipped += int(res.get("skipped", 0) or 0)
            except Exception as exc:
                errors.append(str(exc))

        delta_bytes = gb * (1024**3)
        updated_clients = []

        for u in local:
            client = Client.query.filter_by(inbound_tag=u["tag"], email=u["email"]).first()
            if not client or client.limit_bytes == 0:
                skipped += 1
                continue
            if mode == "add":
                client.limit_bytes = client.limit_bytes + delta_bytes
            else:
                new_limit = client.limit_bytes - delta_bytes
                if new_limit <= 0:
                    skipped += 1
                    continue
                client.limit_bytes = new_limit
            updated_clients.append(client)

        if updated_clients:
            generate_config_file()
            db.session.commit()

            for client in updated_clients:
                try:
                    sub_cache.invalidate_user(client.id)
                except Exception:
                    pass

        updated += len(updated_clients)
        resp = {"status": "ok", "updated": updated, "skipped": skipped}
        if errors:
            resp["errors"] = errors
        return jsonify(resp), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/users/bulk-set-flow", methods=["POST"])
@admin_or_federation_token_required
@limiter.limit("60 per minute")
def bulk_set_flow_route():

    try:
        from panel_core.services.panel_proxy import proxy_bulk_set_flow

        data = request.get_json(silent=True) or {}
        if "flow" not in data:
            raise ValueError("flow field required")
        flow = str(data.get("flow") or "").strip()
        if flow not in ALLOWED_VLESS_FLOWS:
            raise ValueError("flow must be '' or 'xtls-rprx-vision'")
        local, remote = _split_users_by_panel(data.get("users"))

        updated = 0
        skipped = 0
        errors = []
        for panel_id, group in remote.items():
            try:
                res = proxy_bulk_set_flow(panel_id, group, flow)
                updated += int(res.get("updated", 0) or 0)
                skipped += int(res.get("skipped", 0) or 0)
            except Exception as exc:
                errors.append(str(exc))

        changed = []
        for u in local:
            client = Client.query.filter_by(inbound_tag=u["tag"], email=u["email"]).first()
            if not client:
                skipped += 1
                continue
            ib = Inbound.query.filter_by(tag=u["tag"]).first()
            if not ib or ib.protocol != "vless":
                skipped += 1
                continue

            if flow and not inbound_supports_vless_flow(ib):
                skipped += 1
                continue
            if (client.flow or "") == flow:
                skipped += 1
                continue
            changed.append({"client": client, "old_email": client.email, "was_enabled": bool(client.enable)})
            client.flow = flow

        if changed:
            generate_config_file()
            db.session.commit()

            for item in changed:
                try:
                    sub_cache.invalidate_user(item["client"].id)
                except Exception:
                    pass

            grpc_failed = False
            for item in changed:
                if not item["was_enabled"]:
                    continue
                client = item["client"]
                if not _api_remove_user_grpc(client.inbound_tag, item["old_email"]):
                    grpc_failed = True
                if not _api_add_user_grpc(client.inbound_tag, client):
                    grpc_failed = True
            if grpc_failed:
                restart_xray_container()

        updated += len(changed)
        resp = {"status": "ok", "updated": updated, "skipped": skipped}
        if errors:
            resp["errors"] = errors
        return jsonify(resp), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/clients/<client_id>/devices", methods=["GET"])
@token_required
def admin_list_devices(client_id):
    client = db.session.get(Client, client_id)
    if not client:
        return jsonify({"error": "Not found"}), 404
    from panel_core.services.device_tracking import list_devices

    devices = list_devices(client_id)
    return jsonify([d.to_dict(include_admin_fields=True) for d in devices])


@bp.route("/clients/<client_id>/devices/<int:device_id>", methods=["DELETE"])
@token_required
def admin_revoke_device(client_id, device_id):
    client = db.session.get(Client, client_id)
    if not client:
        return jsonify({"error": "Not found"}), 404
    from panel_core.services.device_tracking import revoke_device

    if not revoke_device(client_id, device_id):
        return jsonify({"error": "Not found"}), 404
    try:
        sub_cache.invalidate_user(client_id)
    except Exception:
        pass
    return ("", 204)
