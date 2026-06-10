import json
from flask import Blueprint, request, jsonify
from app.extensions import db, limiter
from app.models import RoutingProfile, Outbound, Balancer
from app.utils import token_required
from app.services.xray import generate_config_file, restart_xray_container

bp = Blueprint("routing", __name__)

MAX_PROFILE_NAME_LEN = 50
MAX_RULE_TEXT_LEN = 128
MAX_RULE_COMMENT_LEN = 255
LIST_FIELDS = {"domain", "ip", "source", "protocol", "user", "inboundTag"}
IP_ONLY_PREFIXES = ("geoip:",)
DOMAIN_ONLY_PREFIXES = ("geosite:", "domain:", "regexp:", "keyword:", "full:", "dotless:")
ALLOWED_PROTOCOLS = ("http", "tls", "quic", "bittorrent")
ALLOWED_NETWORKS = ("tcp", "udp")
TRUTHY_VALUES = {"1", "true", "yes", "on"}
FALSY_VALUES = {"0", "false", "no", "off"}


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


def _normalize_profile_name(raw_name):
    name = str(raw_name or "").strip()
    if not name:
        raise ValueError("Profile name is required")
    if len(name) > MAX_PROFILE_NAME_LEN:
        raise ValueError(f"Profile name too long (max {MAX_PROFILE_NAME_LEN})")
    return name


def _normalize_text(raw_value, max_len):
    value = str(raw_value or "").strip()
    if not value:
        return ""
    return value[:max_len]


def _normalize_list(raw_value):
    if raw_value is None:
        return []

    values = raw_value if isinstance(raw_value, list) else [raw_value]
    result = []
    for item in values:
        normalized = str(item).strip()
        if normalized:
            result.append(normalized)
    return result


def _has_prefix(entry, prefixes):
    value = entry[1:] if entry.startswith("!") else entry
    return value.lower().startswith(prefixes)


def _validate_rule_field_prefixes(rule, idx):
    for entry in rule.get("domain", []):
        if _has_prefix(entry, IP_ONLY_PREFIXES):
            raise ValueError(f'Rule #{idx}: "{entry}" is an IP category — move it to the IPS field, not Domains')
    for field, label in (("ip", "IPS"), ("source", "Source IP")):
        for entry in rule.get(field, []):
            if _has_prefix(entry, DOMAIN_ONLY_PREFIXES):
                raise ValueError(
                    f'Rule #{idx}: "{entry}" is a domain matcher — move it to the Domains field, not {label}'
                )
    for entry in rule.get("protocol", []):
        if entry.lower() not in ALLOWED_PROTOCOLS:
            raise ValueError(f'Rule #{idx}: unknown protocol "{entry}" — allowed: {", ".join(ALLOWED_PROTOCOLS)}')
    for token in rule.get("network", "").split(","):
        token = token.strip().lower()
        if token and token not in ALLOWED_NETWORKS:
            raise ValueError(f'Rule #{idx}: unknown network "{token}" — allowed: {", ".join(ALLOWED_NETWORKS)}')


def _normalize_rules(raw_rules):
    if raw_rules is None:
        return []
    if not isinstance(raw_rules, list):
        raise ValueError("Rules must be an array")

    normalized_rules = []
    for idx, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"Rule #{idx + 1} must be an object")

        rule = {"type": "field"}
        rule["enabled"] = _parse_bool(raw_rule.get("enabled"), True)

        for field in LIST_FIELDS:
            values = _normalize_list(raw_rule.get(field))
            if values:
                rule[field] = values

        for field in ("port", "network"):
            value = _normalize_text(raw_rule.get(field), MAX_RULE_TEXT_LEN)
            if value:
                rule[field] = value

        comment = _normalize_text(raw_rule.get("comment"), MAX_RULE_COMMENT_LEN)
        if comment:
            rule["comment"] = comment

        target = _normalize_text(
            raw_rule.get("outboundTag") or raw_rule.get("balancerTag"),
            MAX_RULE_TEXT_LEN,
        )
        if not target:
            raise ValueError(f"Rule #{idx + 1} requires outbound target")

        rule["outboundTag"] = target

        _validate_rule_field_prefixes(rule, idx + 1)
        normalized_rules.append(rule)

    return normalized_rules


def _ensure_rule_targets_exist(rules):
    if not rules:
        return
    allowed_targets = {item.tag for item in Outbound.query.all()}
    allowed_targets.update({item.tag for item in Balancer.query.all()})

    for idx, rule in enumerate(rules, start=1):
        target = str(rule.get("outboundTag") or rule.get("balancerTag") or "").strip()
        if not target:
            raise ValueError(f"Rule #{idx} requires outbound target")
        if target not in allowed_targets:
            raise ValueError(f"Rule #{idx} has unknown outbound target: {target}")


@bp.route("/routing-profiles", methods=["GET"])
@token_required
def get_profiles():
    profiles = RoutingProfile.query.all()
    result = []
    for profile in profiles:
        try:
            rules = json.loads(profile.rules) if profile.rules else []
        except Exception:
            rules = []
        result.append(
            {
                "id": profile.id,
                "name": profile.name,
                "enable": bool(profile.enable),
                "rules": rules,
            }
        )
    return jsonify(result)


@bp.route("/routing-profiles", methods=["POST"])
@token_required
@limiter.limit("30 per minute")
def create_profile():
    data = request.get_json(silent=True) or {}
    try:
        name = _normalize_profile_name(data.get("name"))
        enabled = _parse_bool(data.get("enable"), True)
        rules = _normalize_rules(data.get("rules", []))
        _ensure_rule_targets_exist(rules)

        if RoutingProfile.query.filter_by(name=name).first():
            raise ValueError("Name exists")

        p = RoutingProfile(
            name=name,
            enable=enabled,
            rules=json.dumps(rules, ensure_ascii=False),
        )
        db.session.add(p)
        db.session.commit()
        return jsonify({"id": p.id, "name": p.name, "enable": bool(p.enable)}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/routing-profiles/<int:pid>", methods=["PUT"])
@token_required
@limiter.limit("30 per minute")
def update_profile(pid):
    try:
        p = db.session.get(RoutingProfile, pid)
        if not p:
            return jsonify({"error": "Not found"}), 404

        data = request.get_json(silent=True) or {}
        if "name" in data:
            new_name = _normalize_profile_name(data.get("name"))
            duplicate = RoutingProfile.query.filter(RoutingProfile.name == new_name, RoutingProfile.id != pid).first()
            if duplicate:
                raise ValueError("Name exists")
            p.name = new_name
        if "enable" in data:
            p.enable = _parse_bool(data.get("enable"), bool(p.enable))
        if "rules" in data:
            rules = _normalize_rules(data["rules"])
            _ensure_rule_targets_exist(rules)
            p.rules = json.dumps(rules, ensure_ascii=False)

        generate_config_file()
        db.session.commit()
        restart_xray_container()
        return jsonify({"status": "updated"}), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/routing-profiles/<int:pid>", methods=["DELETE"])
@token_required
@limiter.limit("30 per minute")
def delete_profile(pid):
    try:
        p = db.session.get(RoutingProfile, pid)
        if not p:
            return jsonify({"error": "Not found"}), 404
        for ib in p.inbounds:
            ib.routing_profile_id = None
        db.session.delete(p)
        generate_config_file()
        db.session.commit()
        restart_xray_container()
        return jsonify({"status": "deleted"}), 200
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500
