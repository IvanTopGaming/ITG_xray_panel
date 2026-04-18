from flask import Blueprint, request, jsonify
from app.extensions import db
from app.models import Node, Inbound, SystemSetting
from app.utils import token_required
from app.services import sub_cache
from app.services.node_sync import (
    NodeClient,
    node_user_sync_job,
    node_inbound_sync_job,
    sync_inbound_to_node,
    reconcile_users_on_node,
)

bp = Blueprint("nodes", __name__)

MASTER_GROUPS_KEY = "master_groups"


def _normalize_groups_csv(value):
    """Validate + normalize a list/CSV of group tags into a sorted CSV string.

    Tags must be 1-30 chars: alphanumeric, '-', '_'. Empty input → "".
    """
    if isinstance(value, list):
        items = [str(g).strip() for g in value if str(g).strip()]
    else:
        items = [g.strip() for g in str(value or "").split(",") if g.strip()]
    for g in items:
        if len(g) > 30 or not all(ch.isalnum() or ch in "-_" for ch in g):
            raise ValueError("Group tags must be 1-30 chars: letters, digits, '-', '_'")
    return ",".join(sorted(set(items)))


def _get_master_groups_csv():
    setting = SystemSetting.query.filter_by(key=MASTER_GROUPS_KEY).first()
    return (setting.value if setting else "") or ""


def _validate_node_data(data, existing_id=None):
    name = str(data.get("name", "") or "").strip()
    if not name or len(name) > 50:
        raise ValueError("Name is required (max 50 chars)")

    dup = Node.query.filter_by(name=name).first()
    if dup and dup.id != existing_id:
        raise ValueError("Node name already exists")

    url = str(data.get("url", "") or "").strip().rstrip("/")
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("URL must start with http:// or https://")

    username = str(data.get("username", "") or "").strip()
    if not username:
        raise ValueError("Username is required")

    password = str(data.get("password", "") or "").strip()
    if not password:
        raise ValueError("Password is required")

    inbound_tag = str(data.get("inbound_tag", "") or "").strip()
    if not inbound_tag:
        raise ValueError("Inbound tag is required")

    enable = bool(data.get("enable", True))
    sync_users = bool(data.get("sync_users", True))
    sync_inbound = bool(data.get("sync_inbound", True))
    strict_mirror = bool(data.get("strict_mirror", False))

    groups_csv = _normalize_groups_csv(data.get("groups", ""))

    return {
        "name": name,
        "url": url,
        "username": username,
        "password": password,
        "inbound_tag": inbound_tag,
        "enable": enable,
        "sync_users": sync_users,
        "sync_inbound": sync_inbound,
        "strict_mirror": strict_mirror,
        "groups": groups_csv,
    }


@bp.route("/nodes", methods=["GET"])
@token_required
def list_nodes():
    include_password = str(request.args.get("include_password") or "").lower() in {"1", "true", "yes"}
    nodes = Node.query.order_by(Node.id).all()
    return jsonify([n.to_dict(mask_password=not include_password) for n in nodes]), 200


def _master_dict():
    csv = _get_master_groups_csv()
    groups = [g.strip() for g in csv.split(",") if g.strip()]
    return {"groups": groups}


@bp.route("/nodes/master", methods=["GET"])
@token_required
def get_master():
    """Return tags assigned to the master panel itself.

    Master is the local instance — it has no Node row, but its tags participate
    in user access filtering exactly like remote node tags.
    """
    return jsonify(_master_dict()), 200


@bp.route("/nodes/master", methods=["PUT"])
@token_required
def update_master():
    try:
        data = request.get_json(silent=True) or {}
        groups_csv = _normalize_groups_csv(data.get("groups", ""))
        setting = SystemSetting.query.filter_by(key=MASTER_GROUPS_KEY).first()
        if setting:
            setting.value = groups_csv
        else:
            db.session.add(SystemSetting(key=MASTER_GROUPS_KEY, value=groups_csv))
        db.session.commit()
        # Visibility of master in subscriptions changed → drop cached responses.
        sub_cache.invalidate_all_users()
        return jsonify(_master_dict()), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/nodes", methods=["POST"])
@token_required
def create_node():
    try:
        data = request.get_json(silent=True) or {}
        validated = _validate_node_data(data)
        node = Node(**validated)
        db.session.add(node)
        db.session.commit()
        return jsonify(node.to_dict(mask_password=True)), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/nodes/<int:node_id>", methods=["PUT"])
@token_required
def update_node(node_id):
    try:
        node = db.session.get(Node, node_id)
        if not node:
            return jsonify({"error": "Node not found"}), 404

        data = request.get_json(silent=True) or {}

        password = str(data.get("password", "") or "").strip()
        if not password or password == "••••••••":
            data["password"] = node.password

        validated = _validate_node_data(data, existing_id=node_id)
        for key, value in validated.items():
            setattr(node, key, value)
        db.session.commit()
        return jsonify(node.to_dict(mask_password=True)), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/nodes/<int:node_id>", methods=["DELETE"])
@token_required
def delete_node(node_id):
    try:
        node = db.session.get(Node, node_id)
        if not node:
            return jsonify({"error": "Node not found"}), 404
        db.session.delete(node)
        db.session.commit()
        return jsonify({"status": "deleted"}), 200
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/nodes/<int:node_id>/test", methods=["POST"])
@token_required
def test_node(node_id):
    try:
        node = db.session.get(Node, node_id)
        if not node:
            return jsonify({"error": "Node not found"}), 404

        client = NodeClient(node)

        if not client.login():
            return jsonify({"online": False, "error": "Authentication failed"}), 200

        result = client.health_check()

        node.status = "online" if result["online"] else "offline"
        node.last_check = int(__import__("time").time() * 1000)
        node.last_error = result.get("error", "")
        db.session.commit()

        return jsonify(result), 200
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/nodes/sync", methods=["POST"])
@token_required
def trigger_sync():
    try:
        node_user_sync_job()
        return jsonify({"status": "sync_complete"}), 200
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/nodes/sync-inbounds", methods=["POST"])
@token_required
def trigger_inbound_sync():
    try:
        node_inbound_sync_job()
        return jsonify({"status": "sync_complete"}), 200
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


@bp.route("/nodes/<int:node_id>/sync-inbound", methods=["POST"])
@token_required
def sync_node_inbound(node_id):
    """Push the master inbound to a node and reconcile its users.

    By default the inbound is only created if it does not already exist on the node —
    a remote inbound that is already present is left untouched, so admins can keep
    node-side tweaks. Pass ?force=1 to overwrite the remote inbound from master.
    Users are always reconciled regardless.
    """
    try:
        node = db.session.get(Node, node_id)
        if not node:
            return jsonify({"error": "Node not found"}), 404
        ib = Inbound.query.filter_by(tag=node.inbound_tag).first()
        if not ib:
            return jsonify({"error": f"No master inbound with tag '{node.inbound_tag}'"}), 404

        force = request.args.get("force", "").lower() in ("1", "true", "yes")

        body, status, written = sync_inbound_to_node(node, ib, force=force)
        if status not in (200, 201):
            return jsonify({"status": "failed", "remote": body, "remote_status": status}), 502

        user_stats = reconcile_users_on_node(node)
        return (
            jsonify(
                {
                    "status": "synced",
                    "inbound_written": written,
                    "forced": force,
                    "remote": body,
                    "users": user_stats,
                }
            ),
            200,
        )
    except Exception:
        return jsonify({"error": "Internal server error"}), 500
