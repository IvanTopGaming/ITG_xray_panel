import ipaddress
import json
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, request, jsonify
from panel_core.extensions import db, limiter
from panel_core.models import Outbound, Balancer, Client
from panel_core.utils import (
    admin_or_federation_token_required,
    audit_privileged_change,
    normalize_tag,
    remote_panel_failure,
    token_required,
)
from panel_core.services.egress import allocate_bind_ip
from panel_core.xray.facade import generate_config_file, has_local_xray, restart_xray_container

logger = logging.getLogger(__name__)

bp = Blueprint("outbound", __name__)
XRAY_OUTBOUNDS_UNSUPPORTED = (
    "Outbounds are configured on the node that runs Xray; this role has no local Xray instance. "
    "Supply panel_id to route the operation to a node."
)
XRAY_BALANCERS_UNSUPPORTED = (
    "Balancers are configured on the node that runs Xray; this role has no local Xray instance. "
    "Supply panel_id to route the operation to a node."
)
XRAY_OUTBOUND_HEALTH_UNSUPPORTED = (
    "Outbound health is probed from the machine the traffic leaves through; this role has no local Xray "
    "instance. Open the node's own panel to see it."
)
ALLOWED_BALANCER_STRATEGIES = {"random", "leastLoad", "leastPing"}
TRUTHY_VALUES = {"1", "true", "yes", "on"}
FALSY_VALUES = {"0", "false", "no", "off"}


def _normalize_protocol(value):
    protocol = str(value or "").strip()
    if not protocol:
        raise ValueError("Protocol required")
    if len(protocol) > 30:
        raise ValueError("Protocol is too long")
    return protocol


def _parse_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in TRUTHY_VALUES:
        return True
    if raw in FALSY_VALUES:
        return False
    return default


def _normalize_selector(raw_selector):
    if raw_selector is None:
        return []
    if not isinstance(raw_selector, list):
        raise ValueError("selector must be an array")
    selector = []
    for item in raw_selector:
        selector.append(normalize_tag(item, "selector tag"))
    return selector


def _validate_fallback_tag(value, selector_tags):

    if value in [None, ""]:
        return None
    tag = str(value).strip()
    if not tag:
        return None
    known = {ob.tag for ob in Outbound.query.all()}
    if tag not in known:
        raise ValueError(f"Unknown outbound tag for fallback: {tag}")
    if tag in set(selector_tags):
        raise ValueError(f"Fallback outbound '{tag}' must not be in selector")
    return tag


def _normalize_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 0
    if not (1 <= port <= 65535):
        return 0
    return port


def _parse_host_port(raw_value):
    value = str(raw_value or "").strip()
    if not value:
        return "", 0

    if value.startswith("[") and "]:" in value:
        host_part, port_part = value.rsplit("]:", 1)
        host = host_part[1:].strip()
        return host, _normalize_port(port_part.strip())

    if ":" not in value:
        return value, 0

    host, port_part = value.rsplit(":", 1)
    return host.strip(), _normalize_port(port_part.strip())


def _extract_outbound_probe_target(protocol, settings):
    proto = str(protocol or "").strip().lower()
    if proto in {"freedom", "blackhole", "dns"}:
        return "", 0

    if proto in {"vless", "vmess"}:
        vnext = settings.get("vnext", [])
        if isinstance(vnext, list) and vnext:
            first = vnext[0] if isinstance(vnext[0], dict) else {}
            return str(first.get("address", "")).strip(), _normalize_port(first.get("port"))

    if proto in {"trojan", "shadowsocks", "socks", "http"}:
        servers = settings.get("servers", [])
        if isinstance(servers, list) and servers:
            first = servers[0] if isinstance(servers[0], dict) else {}
            return str(first.get("address", "")).strip(), _normalize_port(first.get("port"))

    if proto == "wireguard":
        peers = settings.get("peers", [])
        if isinstance(peers, list) and peers:
            first = peers[0] if isinstance(peers[0], dict) else {}
            return _parse_host_port(first.get("endpoint", ""))

    return "", 0


def _normalize_ip(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(ipaddress.IPv4Address(raw))
    except ValueError:
        raise ValueError("Invalid IPv4 address")


def _assign_egress_fields(ob, protocol, public_ip, gateway, *, current_tag=None):
    if not public_ip:
        ob.public_ip = None
        ob.gateway = None
        ob.send_through = None
        return
    if protocol != "freedom":
        raise ValueError("Dedicated egress IP is only supported on freedom outbounds")
    dup = Outbound.query.filter_by(public_ip=public_ip).first()
    if dup and dup.tag != current_tag:
        raise ValueError("public_ip already assigned to another outbound")
    ob.public_ip = public_ip
    ob.gateway = gateway or None
    if not ob.send_through:
        used = [o.send_through for o in Outbound.query.filter(Outbound.send_through.isnot(None)).all()]
        ob.send_through = allocate_bind_ip(used)


def _probe_outbound(host, port, timeout_sec=1.5):
    started = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout_sec):
        elapsed = (time.perf_counter() - started) * 1000
        return max(1, int(elapsed))


def _requested_panel_id():
    return request.args.get("panel_id", type=int)


@bp.route("/outbounds", methods=["GET"])
@admin_or_federation_token_required
def get_outbounds():
    panel_id = _requested_panel_id()
    if panel_id:
        from panel_core.services.panel_proxy import RemotePanelError, proxy_list_outbounds

        try:
            return jsonify(proxy_list_outbounds(panel_id))
        except RemotePanelError as exc:
            return remote_panel_failure(exc)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    outbounds = Outbound.query.all()
    return jsonify(
        [
            {
                "tag": o.tag,
                "protocol": o.protocol,
                "enable": bool(getattr(o, "enable", True)),
                "settings": json.loads(o.settings),
                "streamSettings": json.loads(o.stream_settings),
                "mux": json.loads(o.mux),
                "send_through": o.send_through or "",
                "public_ip": o.public_ip or "",
                "gateway": o.gateway or "",
            }
            for o in outbounds
        ]
    )


@bp.route("/outbounds/health", methods=["GET"])
@token_required
def get_outbounds_health():
    if not has_local_xray():
        return jsonify({"error": XRAY_OUTBOUND_HEALTH_UNSUPPORTED}), 501
    checked_at = int(time.time() * 1000)
    outbounds = Outbound.query.all()
    result = []
    pending_probes = []

    for outbound in outbounds:
        status = {
            "tag": outbound.tag,
            "status": "unknown",
            "rttMs": None,
            "checkedAt": checked_at,
            "endpoint": "",
            "error": "",
        }

        try:
            settings = json.loads(outbound.settings or "{}")
            if not isinstance(settings, dict):
                settings = {}
        except Exception:
            settings = {}
            status["status"] = "unknown"
            status["error"] = "invalid settings"
            result.append(status)
            continue

        host, port = _extract_outbound_probe_target(outbound.protocol, settings)
        if not host or not port:
            status["status"] = "unknown"
            status["error"] = "unsupported or missing endpoint"
            result.append(status)
            continue

        status["endpoint"] = f"{host}:{port}"
        result.append(status)
        pending_probes.append((status, host, port))

    if pending_probes:
        worker_count = min(32, max(1, len(pending_probes)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_status = {
                executor.submit(_probe_outbound, host, port): status for status, host, port in pending_probes
            }
            for future in as_completed(future_to_status):
                status = future_to_status[future]
                try:
                    status["rttMs"] = future.result()
                    status["status"] = "up"
                except OSError as exc:
                    status["status"] = "down"
                    status["error"] = str(exc)
                except Exception as exc:
                    status["status"] = "down"
                    status["error"] = str(exc)

    return jsonify(result)


@bp.route("/outbounds", methods=["POST"])
@admin_or_federation_token_required
@limiter.limit("30 per minute")
def create_outbound():
    panel_id = _requested_panel_id()
    if panel_id:
        from panel_core.services.panel_proxy import RemotePanelError, proxy_create_outbound

        try:
            return jsonify(proxy_create_outbound(panel_id, request.get_json(silent=True) or {})), 201
        except RemotePanelError as exc:
            return remote_panel_failure(exc)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    if not has_local_xray():
        return jsonify({"error": XRAY_OUTBOUNDS_UNSUPPORTED}), 501
    data = request.get_json(silent=True) or {}
    try:
        tag = normalize_tag(data.get("tag"))
        if tag in ["api", "direct", "block"]:
            raise ValueError(f"Tag '{tag}' is reserved")
        protocol = _normalize_protocol(data.get("protocol"))
        enabled = _parse_bool(data.get("enable"), True)
        if Outbound.query.filter_by(tag=tag).first():
            raise ValueError("Tag exists")

        settings = data.get("settings", {})
        stream_settings = data.get("streamSettings", {})
        mux = data.get("mux", {})
        if not isinstance(settings, dict):
            raise ValueError("settings must be an object")
        if not isinstance(stream_settings, dict):
            raise ValueError("streamSettings must be an object")
        if not isinstance(mux, dict):
            raise ValueError("mux must be an object")

        new_ob = Outbound(
            tag=tag,
            protocol=protocol,
            enable=enabled,
            settings=json.dumps(settings),
            stream_settings=json.dumps(stream_settings),
            mux=json.dumps(mux),
        )
        public_ip = _normalize_ip(data.get("public_ip"))
        gateway = _normalize_ip(data.get("gateway"))
        _assign_egress_fields(new_ob, protocol, public_ip, gateway)
        db.session.add(new_ob)
        generate_config_file()
        db.session.commit()
        restart_xray_container()
        audit_privileged_change(logger, f"outbound '{new_ob.tag}' created")
        return jsonify({"tag": new_ob.tag}), 201
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/outbounds/<tag>", methods=["PUT"])
@admin_or_federation_token_required
@limiter.limit("30 per minute")
def update_outbound(tag):
    panel_id = _requested_panel_id()
    if panel_id:
        from panel_core.services.panel_proxy import RemotePanelError, proxy_update_outbound

        try:
            return jsonify(proxy_update_outbound(panel_id, tag, request.get_json(silent=True) or {}))
        except RemotePanelError as exc:
            return remote_panel_failure(exc)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    if not has_local_xray():
        return jsonify({"error": XRAY_OUTBOUNDS_UNSUPPORTED}), 501
    try:
        ob = Outbound.query.filter_by(tag=tag).first()
        if not ob:
            return jsonify({"error": "Not found"}), 404
        if tag in ["direct", "block"]:
            raise ValueError("System outbound cannot be modified")
        data = request.get_json(silent=True) or {}
        if "protocol" in data:
            ob.protocol = _normalize_protocol(data.get("protocol"))
        if "settings" in data:
            if not isinstance(data["settings"], dict):
                raise ValueError("settings must be an object")
            ob.settings = json.dumps(data["settings"])
        if "streamSettings" in data:
            if not isinstance(data["streamSettings"], dict):
                raise ValueError("streamSettings must be an object")
            ob.stream_settings = json.dumps(data["streamSettings"])
        if "mux" in data:
            if not isinstance(data["mux"], dict):
                raise ValueError("mux must be an object")
            ob.mux = json.dumps(data["mux"])
        if "enable" in data:
            ob.enable = _parse_bool(data.get("enable"), bool(getattr(ob, "enable", True)))
        if "public_ip" in data or "gateway" in data:
            public_ip = _normalize_ip(data["public_ip"]) if "public_ip" in data else (ob.public_ip or "")
            gateway = _normalize_ip(data["gateway"]) if "gateway" in data else (ob.gateway or "")
            _assign_egress_fields(ob, ob.protocol, public_ip, gateway, current_tag=ob.tag)
        generate_config_file()
        db.session.commit()
        restart_xray_container()
        audit_privileged_change(logger, f"outbound '{tag}' updated")
        return jsonify({"status": "updated"}), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/outbounds/<tag>", methods=["DELETE"])
@admin_or_federation_token_required
@limiter.limit("30 per minute")
def delete_outbound(tag):
    panel_id = _requested_panel_id()
    if panel_id:
        from panel_core.services.panel_proxy import RemotePanelError, proxy_delete_outbound

        try:
            return jsonify(proxy_delete_outbound(panel_id, tag))
        except RemotePanelError as exc:
            return remote_panel_failure(exc)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    if not has_local_xray():
        return jsonify({"error": XRAY_OUTBOUNDS_UNSUPPORTED}), 501
    try:
        if tag in ["direct", "block"]:
            raise ValueError("System outbound")
        ob = Outbound.query.filter_by(tag=tag).first()
        if not ob:
            return jsonify({"error": "Not found"}), 404

        balancers = Balancer.query.all()
        dependent_selectors = []
        dependent_fallbacks = []
        for balancer in balancers:
            selector = json.loads(balancer.selector) if balancer.selector else []
            if tag in selector:
                dependent_selectors.append(balancer.tag)
            if balancer.fallback_tag == tag:
                dependent_fallbacks.append(balancer.tag)
        errors = []
        if dependent_selectors:
            errors.append("used by balancers (selector): " + ", ".join(dependent_selectors))
        if dependent_fallbacks:
            errors.append("used by balancers (fallback): " + ", ".join(dependent_fallbacks))
        if errors:
            raise ValueError("Outbound is " + "; ".join(errors))

        clients = Client.query.filter_by(preferred_outbound=tag).all()
        for client in clients:
            client.preferred_outbound = None

        db.session.delete(ob)
        generate_config_file()
        db.session.commit()
        restart_xray_container()
        audit_privileged_change(logger, f"outbound '{tag}' deleted")
        return jsonify({"status": "deleted"}), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/balancers", methods=["GET"])
@admin_or_federation_token_required
def get_balancers():
    panel_id = _requested_panel_id()
    if panel_id:
        from panel_core.services.panel_proxy import RemotePanelError, proxy_list_balancers

        try:
            return jsonify(proxy_list_balancers(panel_id))
        except RemotePanelError as exc:
            return remote_panel_failure(exc)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    balancers = Balancer.query.all()
    return jsonify(
        [
            {
                "tag": b.tag,
                "enable": bool(getattr(b, "enable", True)),
                "selector": json.loads(b.selector),
                "strategy": b.strategy,
                "fallback_tag": b.fallback_tag,
            }
            for b in balancers
        ]
    )


@bp.route("/balancers", methods=["POST"])
@admin_or_federation_token_required
@limiter.limit("30 per minute")
def create_balancer():
    panel_id = _requested_panel_id()
    if panel_id:
        from panel_core.services.panel_proxy import RemotePanelError, proxy_create_balancer

        try:
            return jsonify(proxy_create_balancer(panel_id, request.get_json(silent=True) or {})), 201
        except RemotePanelError as exc:
            return remote_panel_failure(exc)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    if not has_local_xray():
        return jsonify({"error": XRAY_BALANCERS_UNSUPPORTED}), 501
    data = request.get_json(silent=True) or {}
    try:
        tag = normalize_tag(data.get("tag"))
        if Balancer.query.filter_by(tag=tag).first():
            raise ValueError("Tag exists")
        if tag == "system_auto_balancer":
            raise ValueError("Tag 'system_auto_balancer' is reserved")

        selector = _normalize_selector(data.get("selector", []))
        if not selector:
            raise ValueError("selector cannot be empty")
        known_outbounds = {outbound.tag for outbound in Outbound.query.all()}
        missing_tags = [item for item in selector if item not in known_outbounds]
        if missing_tags:
            raise ValueError(f"Unknown outbound tags in selector: {', '.join(missing_tags)}")
        strategy = str(data.get("strategy", "random")).strip() or "random"
        if strategy not in ALLOWED_BALANCER_STRATEGIES:
            raise ValueError("Invalid strategy")
        enabled = _parse_bool(data.get("enable"), True)
        fallback_tag = _validate_fallback_tag(data.get("fallback_tag"), selector)

        new_bal = Balancer(
            tag=tag,
            enable=enabled,
            selector=json.dumps(selector),
            strategy=strategy,
            fallback_tag=fallback_tag,
        )
        db.session.add(new_bal)
        generate_config_file()
        db.session.commit()
        restart_xray_container()
        audit_privileged_change(logger, f"balancer '{new_bal.tag}' created")
        return jsonify({"tag": new_bal.tag}), 201
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/balancers/<tag>", methods=["PUT"])
@admin_or_federation_token_required
@limiter.limit("30 per minute")
def update_balancer(tag):
    panel_id = _requested_panel_id()
    if panel_id:
        from panel_core.services.panel_proxy import RemotePanelError, proxy_update_balancer

        try:
            return jsonify(proxy_update_balancer(panel_id, tag, request.get_json(silent=True) or {}))
        except RemotePanelError as exc:
            return remote_panel_failure(exc)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    if not has_local_xray():
        return jsonify({"error": XRAY_BALANCERS_UNSUPPORTED}), 501
    try:
        bal = Balancer.query.filter_by(tag=tag).first()
        if not bal:
            return jsonify({"error": "Not found"}), 404
        if tag == "system_auto_balancer":
            raise ValueError("Tag 'system_auto_balancer' is reserved")

        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            raise ValueError("Invalid request payload")

        if "selector" in data:
            selector = _normalize_selector(data.get("selector"))
        else:
            selector = json.loads(bal.selector) if bal.selector else []
        if not selector:
            raise ValueError("selector cannot be empty")

        known_outbounds = {outbound.tag for outbound in Outbound.query.all()}
        missing_tags = [item for item in selector if item not in known_outbounds]
        if missing_tags:
            raise ValueError(f"Unknown outbound tags in selector: {', '.join(missing_tags)}")

        if "strategy" in data:
            strategy = str(data.get("strategy", "random")).strip() or "random"
        else:
            strategy = str(bal.strategy or "random").strip() or "random"
        if strategy not in ALLOWED_BALANCER_STRATEGIES:
            raise ValueError("Invalid strategy")

        bal.selector = json.dumps(selector)
        bal.strategy = strategy
        if "fallback_tag" in data:
            bal.fallback_tag = _validate_fallback_tag(data.get("fallback_tag"), selector)
        if "enable" in data:
            bal.enable = _parse_bool(data.get("enable"), bool(getattr(bal, "enable", True)))
        generate_config_file()
        db.session.commit()
        restart_xray_container()
        audit_privileged_change(logger, f"balancer '{tag}' updated")
        return jsonify({"status": "updated"}), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/balancers/<tag>", methods=["DELETE"])
@admin_or_federation_token_required
@limiter.limit("30 per minute")
def delete_balancer(tag):
    panel_id = _requested_panel_id()
    if panel_id:
        from panel_core.services.panel_proxy import RemotePanelError, proxy_delete_balancer

        try:
            return jsonify(proxy_delete_balancer(panel_id, tag))
        except RemotePanelError as exc:
            return remote_panel_failure(exc)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    if not has_local_xray():
        return jsonify({"error": XRAY_BALANCERS_UNSUPPORTED}), 501
    try:
        bal = Balancer.query.filter_by(tag=tag).first()
        if not bal:
            return jsonify({"error": "Not found"}), 404

        clients = Client.query.filter_by(preferred_outbound=tag).all()
        for client in clients:
            client.preferred_outbound = None

        db.session.delete(bal)
        generate_config_file()
        db.session.commit()
        restart_xray_container()
        audit_privileged_change(logger, f"balancer '{tag}' deleted")
        return jsonify({"status": "deleted"}), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500
