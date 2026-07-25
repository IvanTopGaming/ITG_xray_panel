import json
import jwt
import datetime
from flask import Blueprint, request, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from panel_core.extensions import db, limiter
from panel_core.models import Admin, Client, Outbound, Balancer
from panel_core.utils import (
    SECRET_KEY,
    token_required,
    validate_password,
    normalize_email,
    normalize_tag,
)
from panel_core.xray import generate_config_file, restart_xray_container

bp = Blueprint("auth", __name__)


_DUMMY_PASSWORD_HASH = generate_password_hash("constant-time-placeholder")


@bp.route("/login", methods=["POST"])
@limiter.limit("10 per minute")
def login():
    auth = request.get_json(silent=True) or {}
    username = auth.get("username")
    password = auth.get("password")
    if not username or not password:
        return jsonify({"message": "Could not verify"}), 401

    admin = Admin.query.filter_by(username=username).first()
    password_ok = check_password_hash(admin.password if admin else _DUMMY_PASSWORD_HASH, password)
    if admin and password_ok:
        pwd_version = int(admin.password_changed_at or 0)
        token = jwt.encode(
            {
                "user": username,
                "admin_id": admin.id,
                "role": "admin",
                "pwdv": pwd_version,
                "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=2),
            },
            SECRET_KEY,
            algorithm="HS256",
        )
        return jsonify({"token": token, "username": admin.username, "role": "admin"})

    return jsonify({"message": "Invalid credentials"}), 401


@bp.route("/user/routing", methods=["POST"])
@token_required
@limiter.limit("30 per minute")
def set_user_routing():
    data = request.get_json(silent=True) or {}
    try:
        email = normalize_email(data.get("email"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    inbound_tag = data.get("inbound_tag")
    if inbound_tag:
        try:
            inbound_tag = normalize_tag(inbound_tag, "inbound_tag")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    tag = data.get("outbound_tag")
    if tag:
        try:
            tag = normalize_tag(tag, "outbound_tag")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    query = Client.query.filter_by(email=email)
    if inbound_tag:
        query = query.filter_by(inbound_tag=inbound_tag)

    matches = query.all()
    if not matches:
        return jsonify({"error": "User not found"}), 404
    if len(matches) > 1:
        return (
            jsonify(
                {
                    "error": "Ambiguous user email. Provide inbound_tag.",
                    "inbound_tags": sorted({c.inbound_tag for c in matches}),
                }
            ),
            409,
        )

    client = matches[0]

    if tag:
        ob = Outbound.query.filter_by(tag=tag).first()
        bal = Balancer.query.filter_by(tag=tag).first()
        if not ob and not bal and tag != "direct":
            return jsonify({"error": "Invalid outbound tag"}), 400
        if ob and not bool(getattr(ob, "enable", True)):
            return jsonify({"error": "Outbound is disabled"}), 400
        if bal and not bool(getattr(bal, "enable", True)):
            return jsonify({"error": "Balancer is disabled"}), 400
        if bal:
            try:
                selector = json.loads(bal.selector or "[]") if bal.selector else []
            except Exception:
                selector = []
            if not isinstance(selector, list):
                selector = []
            enabled_outbound_tags = {item.tag for item in Outbound.query.filter(Outbound.enable.is_(True)).all()}
            has_active_target = any(str(item).strip() in enabled_outbound_tags for item in selector)
            if not has_active_target:
                return jsonify({"error": "Balancer has no enabled outbounds"}), 400

    if (client.preferred_outbound or "") == (tag or ""):
        return jsonify({"status": "unchanged", "preferred": client.preferred_outbound}), 200

    client.preferred_outbound = tag if tag else None
    db.session.commit()
    generate_config_file()
    restart_xray_container()

    return jsonify({"status": "updated", "preferred": client.preferred_outbound}), 200


@bp.route("/admin/password", methods=["PUT"])
@token_required
def change_password():
    data = request.get_json(silent=True) or {}
    new_password = data.get("new_password")
    if not new_password:
        return jsonify({"error": "New password required"}), 400
    validation_error = validate_password(new_password)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    admin = Admin.query.first()
    if admin:
        admin.password = generate_password_hash(new_password)
        admin.password_changed_at = int(datetime.datetime.utcnow().timestamp())
        db.session.commit()
        return jsonify({"status": "changed"}), 200
    return jsonify({"error": "Admin not found"}), 404
