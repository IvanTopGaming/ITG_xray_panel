"""Admin-facing /api/bot/* endpoints — JWT-protected, drive the panel UI."""

import logging
import os
import secrets
import time
from datetime import datetime, timedelta

import yaml
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

from app.extensions import db
from app.models import (
    BotText,
    Client,
    Inbound,
    LinkedPanel,
    Payment,
    SystemSetting,
    Tariff,
    TariffItem,
    TelegramUser,
    UserTariffAccess,
)
from app.services import bot_events
from app.services.panel_proxy import fetch_panel_snapshot_live, get_panel_snapshot, proxy_update_user
from app.services.provisioning import apply_tariff_for_user, backfill_tariff
from app.services.stats import _api_remove_user_grpc, _api_add_user_grpc
from app.services.xray import generate_config_file, restart_xray_container
from app.utils import token_required

bp = Blueprint("bot_admin", __name__)


def _serialize_item(item):
    return {
        "id": item.id,
        "inbound_tag": item.inbound_tag,
        "label": item.label or "",
        "traffic_gb": item.traffic_gb,
        "panel_id": item.panel_id,
        "sort_order": item.sort_order,
    }


def _serialize_tariff(t):
    return {
        "id": t.id,
        "name": t.name,
        "price_rub": t.price_rub,
        "period_days": t.period_days,
        "visibility": t.visibility,
        "is_trial": t.is_trial,
        "enabled": t.enabled,
        "sort_order": t.sort_order,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "items": [_serialize_item(i) for i in t.items],
    }


@bp.route("/bot/tariffs", methods=["GET"])
@token_required
def list_tariffs():
    tariffs = Tariff.query.order_by(Tariff.sort_order, Tariff.id).all()
    return jsonify({"tariffs": [_serialize_tariff(t) for t in tariffs]})


@bp.route("/bot/tariffs/stats", methods=["GET"])
@token_required
def tariffs_stats():
    """Per-tariff active_subs / revenue_30d / last_sale_at. Tariffs with no activity get zeros."""
    from sqlalchemy import func

    now = datetime.utcnow()
    cutoff_30d = now - timedelta(days=30)

    active_rows = (
        db.session.query(UserTariffAccess.tariff_id, func.count(UserTariffAccess.id))
        .filter((UserTariffAccess.next_renewal_at.is_(None)) | (UserTariffAccess.next_renewal_at > now))
        .group_by(UserTariffAccess.tariff_id)
        .all()
    )
    active_by_tariff = {tid: count for tid, count in active_rows}

    rev_rows = (
        db.session.query(Payment.tariff_id, func.sum(Payment.amount_rub))
        .filter(Payment.status == "succeeded", Payment.paid_at >= cutoff_30d)
        .group_by(Payment.tariff_id)
        .all()
    )
    rev_by_tariff = {tid: int(total or 0) for tid, total in rev_rows}

    last_sale_rows = (
        db.session.query(Payment.tariff_id, func.max(Payment.paid_at))
        .filter(Payment.status == "succeeded", Payment.paid_at.isnot(None))
        .group_by(Payment.tariff_id)
        .all()
    )
    last_sale_by_tariff = {tid: ts for tid, ts in last_sale_rows}

    tariff_ids = [t.id for t in Tariff.query.with_entities(Tariff.id).all()]
    stats = {
        tid: {
            "active_subs": active_by_tariff.get(tid, 0),
            "revenue_30d": rev_by_tariff.get(tid, 0),
            "last_sale_at": (last_sale_by_tariff[tid].isoformat() if last_sale_by_tariff.get(tid) else None),
        }
        for tid in tariff_ids
    }
    return jsonify({"stats": stats})


_VALID_VISIBILITY = frozenset({"public", "private", "archived"})


def _validate_tariff_payload(payload):
    """Raise ValueError on bad input."""
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if len(name) > 120:
        raise ValueError("name too long (max 120)")
    price = payload.get("price_rub")
    if not isinstance(price, int) or price < 0:
        raise ValueError("price_rub must be a non-negative integer")
    period = payload.get("period_days")
    if not isinstance(period, int) or period <= 0:
        raise ValueError("period_days must be a positive integer")
    visibility = payload.get("visibility", "public")
    if visibility not in _VALID_VISIBILITY:
        raise ValueError(f"visibility must be one of {sorted(_VALID_VISIBILITY)}")
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    seen_inbounds = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{i}] must be an object")
        tag = item.get("inbound_tag")
        if not isinstance(tag, str) or not tag.strip():
            raise ValueError(f"items[{i}].inbound_tag is required")
        if tag in seen_inbounds:
            raise ValueError(f"duplicate inbound_tag in items: {tag!r}")
        seen_inbounds.add(tag)
        traffic = item.get("traffic_gb")
        if not isinstance(traffic, int) or traffic < 0:
            raise ValueError(f"items[{i}].traffic_gb must be a non-negative integer")


def _apply_items(tariff, items_payload):
    """Replace tariff.items with the new list. Caller commits."""
    tariff.items.clear()
    db.session.flush()
    for idx, item in enumerate(items_payload):
        tariff.items.append(
            TariffItem(
                inbound_tag=item["inbound_tag"].strip(),
                label=(item.get("label") or "").strip() or None,
                traffic_gb=item["traffic_gb"],
                panel_id=item.get("panel_id"),
                sort_order=item.get("sort_order", idx),
            )
        )


@bp.route("/bot/tariffs", methods=["POST"])
@token_required
def create_tariff():
    payload = request.get_json(silent=True)
    try:
        _validate_tariff_payload(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    is_trial = bool(payload.get("is_trial", False))
    if is_trial:
        existing_trial = Tariff.query.filter_by(is_trial=True).first()
        if existing_trial is not None:
            return jsonify({"error": "a trial tariff already exists"}), 400

    t = Tariff(
        name=payload["name"].strip(),
        price_rub=payload["price_rub"],
        period_days=payload["period_days"],
        visibility=payload.get("visibility", "public"),
        is_trial=is_trial,
        enabled=bool(payload.get("enabled", True)),
        sort_order=payload.get("sort_order", 0),
    )
    db.session.add(t)
    db.session.flush()
    _apply_items(t, payload.get("items", []))
    db.session.commit()
    return jsonify(_serialize_tariff(t)), 201


@bp.route("/bot/tariffs/<int:tariff_id>", methods=["PUT"])
@token_required
def update_tariff(tariff_id):
    t = db.session.get(Tariff, tariff_id)
    if t is None:
        return jsonify({"error": "tariff not found"}), 404

    payload = request.get_json(silent=True)
    try:
        _validate_tariff_payload(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Preserve current is_trial / enabled when caller omits the key, so a
    # partial PUT doesn't silently strip flags. Frontend always sends them,
    # but external API consumers could trip on a default-False contract.
    is_trial = bool(payload.get("is_trial", t.is_trial))
    if is_trial:
        other_trial = Tariff.query.filter(
            Tariff.is_trial == True,  # noqa: E712 — SQLAlchemy idiom
            Tariff.id != tariff_id,
        ).first()
        if other_trial is not None:
            return jsonify({"error": "a trial tariff already exists"}), 400

    t.name = payload["name"].strip()
    t.price_rub = payload["price_rub"]
    t.period_days = payload["period_days"]
    t.visibility = payload.get("visibility", "public")
    t.is_trial = is_trial
    t.enabled = bool(payload.get("enabled", t.enabled))
    t.sort_order = payload.get("sort_order", 0)
    _apply_items(t, payload.get("items", []))
    db.session.commit()

    backfill_summary = None
    try:
        backfill_summary = backfill_tariff(t)
    except Exception:
        db.session.rollback()
        logger.exception("backfill_tariff failed for tariff=%s", t.id)

    return jsonify({**_serialize_tariff(t), "backfill": backfill_summary})


@bp.route("/bot/tariffs/<int:tariff_id>", methods=["DELETE"])
@token_required
def archive_tariff(tariff_id):
    t = db.session.get(Tariff, tariff_id)
    if t is None:
        return jsonify({"error": "tariff not found"}), 404
    t.visibility = "archived"
    db.session.commit()
    return jsonify(_serialize_tariff(t))


@bp.route("/bot/tariffs/<int:tariff_id>/restore", methods=["POST"])
@token_required
def restore_tariff(tariff_id):
    """Bring an archived tariff back to 'public'. No-op on already-visible."""
    t = db.session.get(Tariff, tariff_id)
    if t is None:
        return jsonify({"error": "tariff not found"}), 404
    if t.visibility == "archived":
        t.visibility = "public"
        db.session.commit()
    return jsonify(_serialize_tariff(t))


@bp.route("/bot/tariffs/<int:tariff_id>/permanent", methods=["DELETE"])
@token_required
def delete_tariff_permanent(tariff_id):
    """Hard-delete a tariff. Refuses when Payment rows reference it
    (FK is RESTRICT — preserves billing history). TariffItem and
    UserTariffAccess cascade-delete with the tariff row."""
    t = db.session.get(Tariff, tariff_id)
    if t is None:
        return jsonify({"error": "tariff not found"}), 404
    payment_count = Payment.query.filter_by(tariff_id=tariff_id).count()
    if payment_count > 0:
        return (
            jsonify(
                {
                    "error": "tariff has payment history",
                    "payment_count": payment_count,
                    "hint": "archive the tariff instead — deleting would break billing history",
                }
            ),
            409,
        )
    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/bot/tariffs/<int:tariff_id>/duplicate", methods=["POST"])
@token_required
def duplicate_tariff(tariff_id):
    src = db.session.get(Tariff, tariff_id)
    if src is None:
        return jsonify({"error": "tariff not found"}), 404

    copy = Tariff(
        name=f"{src.name} (копия)",
        price_rub=src.price_rub,
        period_days=src.period_days,
        visibility="public",  # always re-publish a duplicate
        is_trial=False,  # never propagate the trial flag
        enabled=True,
        sort_order=src.sort_order,
    )
    db.session.add(copy)
    db.session.flush()
    for item in src.items:
        copy.items.append(
            TariffItem(
                inbound_tag=item.inbound_tag,
                label=item.label,
                traffic_gb=item.traffic_gb,
                panel_id=item.panel_id,
                sort_order=item.sort_order,
            )
        )
    db.session.commit()
    return jsonify(_serialize_tariff(copy)), 201


_VALID_TEXT_LANGS = frozenset({"ru", "en"})


@bp.route("/bot/texts", methods=["GET"])
@token_required
def list_texts():
    rows = BotText.query.all()
    return jsonify(
        {
            "texts": [
                {
                    "key": r.key,
                    "lang": r.lang,
                    "text": r.text,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]
        }
    )


@bp.route("/bot/texts/keys", methods=["GET"])
@token_required
def list_text_keys():
    """Merge meta + defaults YAML for the editor UI."""
    here = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(here, "..", "data")
    defaults_path = os.path.join(data_dir, "bot_texts_defaults.yaml")
    meta_path = os.path.join(data_dir, "bot_texts_meta.yaml")

    defaults = {}
    if os.path.exists(defaults_path):
        with open(defaults_path, "r", encoding="utf-8") as fh:
            defaults = yaml.safe_load(fh) or {}

    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = yaml.safe_load(fh) or {}

    keys = []
    for key in sorted(defaults.keys()):
        entry = {
            "key": key,
            "description": (meta.get(key) or {}).get("description", ""),
            "variables": (meta.get(key) or {}).get("variables", []),
            "default_ru": (defaults.get(key) or {}).get("ru", ""),
            "default_en": (defaults.get(key) or {}).get("en", ""),
        }
        keys.append(entry)
    return jsonify({"keys": keys})


@bp.route("/bot/texts/<path:key>", methods=["PUT"])
@token_required
def upsert_text(key):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected JSON object"}), 400
    lang = payload.get("lang", "")
    if lang not in _VALID_TEXT_LANGS:
        return jsonify({"error": f"lang must be one of {sorted(_VALID_TEXT_LANGS)}"}), 400
    text = payload.get("text", "")
    if not isinstance(text, str):
        return jsonify({"error": "text must be a string"}), 400

    row = db.session.get(BotText, (key, lang))
    if row is None:
        row = BotText(key=key, lang=lang, text=text)
        db.session.add(row)
    else:
        row.text = text
    db.session.commit()

    bot_events.publish("texts_changed", telegram_id=None, payload={"lang": lang})

    return jsonify(
        {
            "key": row.key,
            "lang": row.lang,
            "text": row.text,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    )


@bp.route("/bot/texts/<path:key>", methods=["DELETE"])
@token_required
def delete_text(key):
    lang = request.args.get("lang", "")
    if lang not in _VALID_TEXT_LANGS:
        return jsonify({"error": f"lang must be one of {sorted(_VALID_TEXT_LANGS)}"}), 400
    row = db.session.get(BotText, (key, lang))
    if row is None:
        return jsonify({"error": "not found"}), 404
    db.session.delete(row)
    db.session.commit()
    bot_events.publish("texts_changed", telegram_id=None, payload={"lang": lang})
    return jsonify({"ok": True})


_VALID_BILLING = frozenset({"free", "paid", "gift"})


def _serialize_grant(g):
    return {
        "id": g.id,
        "telegram_id": g.telegram_id,
        "tariff_id": g.tariff_id,
        "billing": g.billing,
        "next_renewal_at": g.next_renewal_at.isoformat() if g.next_renewal_at else None,
        "note": g.note,
    }


def _remote_clients_by_telegram_id() -> dict[int, list[dict]]:
    """Bucket all linked-panel clients by telegram_id from cached snapshots.

    Includes both enabled and disabled clients (each dict carries its own
    ``enable`` flag) — callers like unblock_user rely on seeing disabled remote
    clients. Each client dict mirrors Client.to_dict() so frontend code can treat
    local and remote rows uniformly, with extra ``panel_id`` / ``panel_name``
    fields so the UI can show where the client lives.

    Returns {} if no panels are linked or all snapshots are missing — callers
    should handle this gracefully (it's the steady-state on a standalone panel).

    NOTE: this reads the *cached* (poll-refreshed, 60s TTL) snapshot and is fine
    for read-only UI rendering. Destructive operations (block/unblock/revoke)
    must use `_remote_clients_by_telegram_id_live` instead, so a stale/missing
    cache can't make a remote disable silently no-op.
    """
    bucket: dict[int, list[dict]] = {}
    for panel in LinkedPanel.query.filter_by(enable=True).all():
        snapshot = get_panel_snapshot(panel.id)
        if not snapshot:
            continue
        _bucket_panel_clients(bucket, snapshot, panel)
    return bucket


def _bucket_panel_clients(bucket: dict[int, list[dict]], snapshot: dict, panel) -> None:
    """Append every telegram-linked client in `panel`'s `snapshot` into `bucket`."""
    for ib_data in snapshot.get("inbounds", []):
        inbound_tag = ib_data.get("tag", "")
        inbound_label = ib_data.get("label") or inbound_tag
        for c in ib_data.get("clients", []):
            tg_id = c.get("telegram_id")
            if not tg_id:
                continue
            bucket.setdefault(tg_id, []).append(
                {
                    "id": c.get("id", ""),
                    "email": c.get("email", ""),
                    "inbound_tag": inbound_tag,
                    "inbound_label": inbound_label,
                    "limit_bytes": c.get("limit_bytes", 0),
                    "expiry_time": c.get("expiry_time", 0),
                    "up": c.get("up", 0),
                    "down": c.get("down", 0),
                    "enable": bool(c.get("enable", True)),
                    "reset_day": c.get("reset_day", 0),
                    "last_reset_time": 0,
                    "last_seen": c.get("last_seen", 0),
                    "source_ips": [],
                    "flow": c.get("flow", ""),
                    "preferred_outbound": "",
                    "device_limit": None,
                    "telegram_id": tg_id,
                    "tariff_id": c.get("tariff_id"),
                    "panel_id": panel.id,
                    "panel_name": panel.name,
                }
            )


def _remote_clients_by_telegram_id_live(
    panel_ids: set[int] | None = None,
) -> tuple[dict[int, list[dict]], list[dict]]:
    """Like `_remote_clients_by_telegram_id` but fetches LIVE snapshots so a
    destructive op never acts on a stale picture.

    Returns ``(bucket, unreachable)`` where `unreachable` is a list of
    ``{panel_id, panel_name, error}`` for every queried panel whose live
    snapshot could not be fetched. Callers fold these into ``panel_failures`` so
    a remote effect we *couldn't apply* is reported instead of silently passing
    as success (the bug class: "revoked the grant, only local clients dropped").

    `panel_ids`: when given, only those linked panels are queried (used by the
    tariff-scoped revoke path); None means every enabled panel.
    """
    bucket: dict[int, list[dict]] = {}
    unreachable: list[dict] = []
    query = LinkedPanel.query.filter_by(enable=True)
    if panel_ids is not None:
        if not panel_ids:
            return bucket, unreachable
        query = query.filter(LinkedPanel.id.in_(panel_ids))
    for panel in query.all():
        try:
            snapshot = fetch_panel_snapshot_live(panel.id)
        except Exception as exc:
            logger.warning("remote enumerate (live) failed for panel=%s: %s", panel.id, exc)
            unreachable.append({"panel_id": panel.id, "panel_name": panel.name, "error": str(exc)})
            continue
        _bucket_panel_clients(bucket, snapshot, panel)
    return bucket, unreachable


def _serialize_user_summary(u, remote_clients_by_tg: dict[int, list[dict]] | None = None):
    local_clients_count = Client.query.filter_by(telegram_id=u.telegram_id).count()
    if remote_clients_by_tg is None:
        remote_clients_by_tg = _remote_clients_by_telegram_id()
    remote_count = len(remote_clients_by_tg.get(u.telegram_id, ()))
    grants_count = UserTariffAccess.query.filter_by(telegram_id=u.telegram_id).count()
    return {
        "telegram_id": u.telegram_id,
        "username": u.username,
        "language": u.language,
        "blocked": u.blocked,
        "first_seen_at": u.first_seen_at.isoformat() if u.first_seen_at else None,
        "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
        "trial_used_at": u.trial_used_at.isoformat() if u.trial_used_at else None,
        "clients_count": local_clients_count + remote_count,
        "grants_count": grants_count,
    }


@bp.route("/bot/users", methods=["GET"])
@token_required
def list_telegram_users():
    users = TelegramUser.query.order_by(TelegramUser.first_seen_at.desc()).all()
    remote_by_tg = _remote_clients_by_telegram_id()
    return jsonify({"users": [_serialize_user_summary(u, remote_by_tg) for u in users]})


@bp.route("/bot/users/<int:tg_id>", methods=["GET"])
@token_required
def get_telegram_user(tg_id):
    user = db.session.get(TelegramUser, tg_id)
    if user is None:
        return jsonify({"error": "telegram user not found"}), 404
    local_clients = Client.query.filter_by(telegram_id=tg_id).all()
    remote_by_tg = _remote_clients_by_telegram_id()
    clients_payload = [c.to_dict() for c in local_clients] + remote_by_tg.get(tg_id, [])
    grants = UserTariffAccess.query.filter_by(telegram_id=tg_id).all()
    payments = Payment.query.filter_by(telegram_id=tg_id).all()
    return jsonify(
        {
            **_serialize_user_summary(user, remote_by_tg),
            "clients": clients_payload,
            "grants": [_serialize_grant(g) for g in grants],
            "payments": [
                {
                    "id": p.id,
                    "yookassa_id": p.yookassa_id,
                    "amount_rub": p.amount_rub,
                    "status": p.status,
                    "tariff_id": p.tariff_id,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                    "paid_at": p.paid_at.isoformat() if p.paid_at else None,
                }
                for p in payments
            ],
        }
    )


@bp.route("/bot/users/<int:tg_id>/grants", methods=["POST"])
@token_required
def create_grant(tg_id):
    payload = request.get_json(silent=True) or {}
    tariff_id = payload.get("tariff_id")
    billing = payload.get("billing")
    note = payload.get("note") or None
    silent = bool(payload.get("silent", False))

    if not isinstance(tariff_id, int):
        return jsonify({"error": "tariff_id (integer) is required"}), 400
    if billing not in _VALID_BILLING:
        return jsonify({"error": f"billing must be one of {sorted(_VALID_BILLING)}"}), 400

    tariff = db.session.get(Tariff, tariff_id)
    if tariff is None:
        return jsonify({"error": "tariff not found"}), 404

    if billing == "paid" and tariff.visibility != "private":
        return jsonify(
            {"error": "'paid' grants are only meaningful for private tariffs (the user can already buy public ones)"}
        ), 400

    user = db.session.get(TelegramUser, tg_id)
    if user is None:
        user = TelegramUser(telegram_id=tg_id, language="ru")
        db.session.add(user)
        db.session.flush()

    grant = UserTariffAccess.query.filter_by(telegram_id=tg_id, tariff_id=tariff_id).first()
    if grant is None:
        grant = UserTariffAccess(
            telegram_id=tg_id,
            tariff_id=tariff_id,
            billing=billing,
            note=note,
        )
        db.session.add(grant)
    else:
        grant.billing = billing
        grant.note = note

    if billing == "free":
        result = apply_tariff_for_user(tg_id, tariff, source="admin_grant")
        grant.next_renewal_at = datetime.utcnow() + timedelta(days=tariff.period_days)
    elif billing == "gift":
        # One-shot admin grant: provision Clients for one tariff period, no
        # auto-renewal. Access expires naturally via the standard
        # Client.expiry_time path enforced by check_limits.
        result = apply_tariff_for_user(tg_id, tariff, source="admin_gift")
        grant.next_renewal_at = None
    else:
        result = None
        grant.next_renewal_at = None

    db.session.commit()

    if not silent:
        if billing == "free" and result is not None:
            bot_events.publish(
                "access_granted",
                tg_id,
                {
                    "tariff_name": tariff.name,
                    "expires_at_ms": result["expires_at_ms"],
                    "lang": user.language or "ru",
                },
            )
        elif billing == "gift" and result is not None:
            bot_events.publish(
                "access_granted_once",
                tg_id,
                {
                    "tariff_name": tariff.name,
                    "expires_at_ms": result["expires_at_ms"],
                    "lang": user.language or "ru",
                },
            )
        elif billing == "paid":
            bot_events.publish(
                "access_offered",
                tg_id,
                {
                    "tariff_name": tariff.name,
                    "lang": user.language or "ru",
                },
            )

    return jsonify(_serialize_grant(grant)), 201


@bp.route("/bot/users/<int:tg_id>/block", methods=["POST"])
@token_required
def block_user(tg_id):
    """Block: bot ignores them, grants cancelled, clients disabled (kept for audit), Xray sessions yanked.

    Three-phase to keep the SQLite writer lock short: classify clients,
    run gRPC removals with no DB writes in flight, then commit all mutations
    in one transaction.
    """
    user = db.session.get(TelegramUser, tg_id)
    if user is None:
        return jsonify({"error": "user not found"}), 404

    active_clients = Client.query.filter_by(telegram_id=tg_id, enable=True).all()
    # Pre-fetch inbounds so the gRPC block doesn't pull in extra SELECTs.
    inbound_tags = {c.inbound_tag for c in active_clients}
    inbounds_by_tag = (
        {ib.tag: ib for ib in Inbound.query.filter(Inbound.tag.in_(inbound_tags)).all()} if inbound_tags else {}
    )

    # ── Phase: gRPC side-effects (no DB writes) ──────────────────────────
    restart_required = False
    for c in active_clients:
        ib = inbounds_by_tag.get(c.inbound_tag)
        if ib and ib.protocol in ("vless", "vmess"):
            try:
                if not _api_remove_user_grpc(c.inbound_tag, c.email):
                    restart_required = True
            except Exception:
                restart_required = True
        else:
            restart_required = True

    # Live snapshot, not the cached one: a stale/missing cache must never let a
    # block silently leave the user enabled on a child panel. Panels we can't
    # reach are surfaced in panel_failures rather than skipped.
    remote_by_tg, panel_failures = _remote_clients_by_telegram_id_live()
    remote_clients = remote_by_tg.get(tg_id, [])
    remote_disabled = 0
    for rc in remote_clients:
        if not rc.get("enable", True):
            continue
        try:
            proxy_update_user(rc["panel_id"], rc["inbound_tag"], {"old_email": rc["email"], "enable": False})
            remote_disabled += 1
        except Exception as exc:
            logger.warning("block: remote disable failed panel=%s tag=%s: %s", rc["panel_id"], rc["inbound_tag"], exc)
            panel_failures.append({"panel_id": rc["panel_id"], "panel_name": rc.get("panel_name"), "error": str(exc)})

    # ── Phase: single short write transaction ────────────────────────────
    user.blocked = True
    for c in active_clients:
        c.enable = False
    cancelled_grants = UserTariffAccess.query.filter_by(telegram_id=tg_id).delete(synchronize_session=False)
    db.session.commit()

    if active_clients:
        generate_config_file()
        if restart_required:
            restart_xray_container()

    try:
        bot_events.publish("user_blocked", telegram_id=tg_id, payload={})
    except Exception:
        pass

    return jsonify(
        {
            "ok": True,
            "telegram_id": tg_id,
            "cancelled_grants": int(cancelled_grants or 0),
            "disabled_clients": len(active_clients),
            "remote_disabled": remote_disabled,
            "panel_failures": panel_failures,
        }
    )


@bp.route("/bot/users/<int:tg_id>/unblock", methods=["POST"])
@token_required
def unblock_user(tg_id):
    """Unblock: clear the flag and re-enable clients whose tariff time still
    remains (local + linked panels). Grants are NOT restored — admin re-grants
    to resume renewal.

    Remote re-enable runs before the commit (best-effort, mirroring block_user),
    while the local gRPC hot-add runs AFTER the commit — matching provisioning —
    so a DB/runtime mismatch fails safe: clients are committed enabled before the
    live Xray runtime starts serving them."""
    user = db.session.get(TelegramUser, tg_id)
    if user is None:
        return jsonify({"error": "user not found"}), 404

    now_ms = int(time.time() * 1000)

    def _has_time(expiry):
        return not expiry or expiry > now_ms

    disabled = Client.query.filter_by(telegram_id=tg_id, enable=False).all()
    to_enable = [c for c in disabled if _has_time(c.expiry_time)]
    inbound_tags = {c.inbound_tag for c in to_enable}
    inbounds_by_tag = (
        {ib.tag: ib for ib in Inbound.query.filter(Inbound.tag.in_(inbound_tags)).all()} if inbound_tags else {}
    )

    # Live snapshot, mirroring block_user — an unreachable panel is reported,
    # not silently treated as "nothing to re-enable here".
    remote_by_tg, panel_failures = _remote_clients_by_telegram_id_live()
    remote_clients = remote_by_tg.get(tg_id, [])
    remote_re_enabled = 0
    for rc in remote_clients:
        if rc.get("enable", True) or not _has_time(rc.get("expiry_time", 0)):
            continue
        try:
            proxy_update_user(rc["panel_id"], rc["inbound_tag"], {"old_email": rc["email"], "enable": True})
            remote_re_enabled += 1
        except Exception as exc:
            logger.warning(
                "unblock: remote re-enable failed panel=%s tag=%s: %s", rc["panel_id"], rc["inbound_tag"], exc
            )
            panel_failures.append({"panel_id": rc["panel_id"], "panel_name": rc.get("panel_name"), "error": str(exc)})

    user.blocked = False
    for c in to_enable:
        c.enable = True
    db.session.commit()

    if to_enable:
        restart_required = False
        for c in to_enable:
            ib = inbounds_by_tag.get(c.inbound_tag)
            if ib and ib.protocol in ("vless", "vmess"):
                try:
                    if not _api_add_user_grpc(c.inbound_tag, c):
                        restart_required = True
                except Exception:
                    restart_required = True
            else:
                restart_required = True
        generate_config_file()
        if restart_required:
            restart_xray_container()

    try:
        bot_events.publish("user_unblocked", telegram_id=tg_id, payload={})
    except Exception:
        pass

    return jsonify(
        {
            "ok": True,
            "telegram_id": tg_id,
            "re_enabled": len(to_enable),
            "remote_re_enabled": remote_re_enabled,
            "panel_failures": panel_failures,
        }
    )


@bp.route("/bot/users/<int:tg_id>/tariffs/<int:tariff_id>", methods=["DELETE"])
@token_required
def revoke_tariff_from_user(tg_id, tariff_id):
    """Revoke one tariff: disable matching clients, gRPC-yank vless/vmess (else regen+restart), drop the grant.

    Three-phase to keep the SQLite writer lock short: classify clients,
    run gRPC removals with no DB writes in flight, then commit in one go.
    """
    active_clients = Client.query.filter_by(telegram_id=tg_id, tariff_id=tariff_id, enable=True).all()
    inbound_tags = {c.inbound_tag for c in active_clients}
    inbounds_by_tag = (
        {ib.tag: ib for ib in Inbound.query.filter(Inbound.tag.in_(inbound_tags)).all()} if inbound_tags else {}
    )

    # ── Phase: gRPC side-effects (no DB writes) ──────────────────────────
    restart_required = False
    for c in active_clients:
        ib = inbounds_by_tag.get(c.inbound_tag)
        if ib and ib.protocol in ("vless", "vmess"):
            try:
                if not _api_remove_user_grpc(c.inbound_tag, c.email):
                    restart_required = True
            except Exception:
                restart_required = True
        else:
            restart_required = True

    # ── Phase: disable this tariff's clients on linked panels (best-effort, no DB writes) ──
    # The tariff's remote footprint is defined by its TariffItems that route to a linked
    # panel (panel_id set). Disable the user's remote clients sitting on those
    # (panel_id, inbound_tag) pairs — mirrors block_user, but scoped to this tariff.
    panel_failures: list[dict] = []
    remote_disabled = 0
    remote_items = (
        TariffItem.query.filter_by(tariff_id=tariff_id)
        .filter(TariffItem.panel_id.isnot(None))
        .with_entities(TariffItem.panel_id, TariffItem.inbound_tag)
        .all()
    )
    wanted = {(pid, tag) for pid, tag in remote_items}
    if wanted:
        # Live snapshot, scoped to the panels this tariff routes to. Panels we
        # can't reach go to panel_failures so a missed remote disable is visible.
        panel_ids = {pid for pid, _tag in remote_items}
        remote_by_tg, unreachable = _remote_clients_by_telegram_id_live(panel_ids=panel_ids)
        panel_failures.extend(unreachable)
        for rc in remote_by_tg.get(tg_id, []):
            # Mirror the local match (tariff_id): only touch THIS tariff's remote
            # clients — not another tariff sharing the same (panel_id, inbound_tag).
            if rc.get("tariff_id") != tariff_id:
                continue
            if (rc.get("panel_id"), rc.get("inbound_tag")) not in wanted or not rc.get("enable", True):
                continue
            try:
                proxy_update_user(rc["panel_id"], rc["inbound_tag"], {"old_email": rc["email"], "enable": False})
                remote_disabled += 1
            except Exception as exc:
                logger.warning(
                    "revoke_tariff: remote disable failed panel=%s tag=%s: %s",
                    rc["panel_id"],
                    rc["inbound_tag"],
                    exc,
                )
                panel_failures.append(
                    {"panel_id": rc["panel_id"], "panel_name": rc.get("panel_name"), "error": str(exc)}
                )

    # ── Phase: single short write transaction ────────────────────────────
    for c in active_clients:
        c.enable = False
        c.tariff_id = None
    revoked_grants = UserTariffAccess.query.filter_by(telegram_id=tg_id, tariff_id=tariff_id).delete(
        synchronize_session=False
    )
    db.session.commit()

    if active_clients:
        generate_config_file()
        if restart_required:
            restart_xray_container()

    return jsonify(
        {
            "ok": True,
            "telegram_id": tg_id,
            "tariff_id": tariff_id,
            "disabled_clients": len(active_clients),
            "revoked_grants": int(revoked_grants or 0),
            "remote_disabled": remote_disabled,
            "panel_failures": panel_failures,
        }
    )


@bp.route("/bot/grants", methods=["GET"])
@token_required
def list_grants():
    rows = (
        db.session.query(UserTariffAccess, TelegramUser, Tariff)
        .outerjoin(TelegramUser, TelegramUser.telegram_id == UserTariffAccess.telegram_id)
        .join(Tariff, Tariff.id == UserTariffAccess.tariff_id)
        .order_by(UserTariffAccess.created_at.desc())
        .all()
    )
    return jsonify(
        {
            "rows": [
                {
                    "id": uta.id,
                    "telegram_id": uta.telegram_id,
                    "username": tu.username if tu else None,
                    "tariff_id": tariff.id,
                    "tariff_name": tariff.name,
                    "billing": uta.billing,
                    "next_renewal_at": uta.next_renewal_at.isoformat() if uta.next_renewal_at else None,
                    "note": uta.note,
                }
                for (uta, tu, tariff) in rows
            ]
        }
    )


def _serialize_payment(p):
    return {
        "id": p.id,
        "yookassa_id": p.yookassa_id,
        "telegram_id": p.telegram_id,
        "tariff_id": p.tariff_id,
        "tariff_name": (p.tariff_snapshot or {}).get("name") or "",
        "amount_rub": p.amount_rub,
        "status": p.status,
        "confirmation_url": p.confirmation_url,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "paid_at": p.paid_at.isoformat() if p.paid_at else None,
    }


@bp.route("/bot/payments", methods=["GET"])
@token_required
def list_payments():
    import datetime as dt
    from sqlalchemy import func

    q = Payment.query.order_by(Payment.created_at.desc())
    status = request.args.get("status")
    if status:
        q = q.filter(Payment.status == status)
    tg_id_raw = request.args.get("telegram_id")
    if tg_id_raw:
        try:
            q = q.filter(Payment.telegram_id == int(tg_id_raw))
        except ValueError:
            pass
    from_date = request.args.get("from")
    to_date = request.args.get("to")
    try:
        if from_date:
            q = q.filter(Payment.created_at >= dt.datetime.fromisoformat(from_date))
        if to_date:
            q = q.filter(Payment.created_at <= dt.datetime.fromisoformat(to_date))
    except ValueError:
        return jsonify({"error": "invalid_date"}), 400

    items = q.limit(500).all()
    total = q.count()

    month_start = dt.datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_count = (
        db.session.query(func.count(Payment.id))
        .filter(Payment.status == "succeeded", Payment.paid_at >= month_start)
        .scalar()
        or 0
    )
    month_amount = (
        db.session.query(func.coalesce(func.sum(Payment.amount_rub), 0))
        .filter(Payment.status == "succeeded", Payment.paid_at >= month_start)
        .scalar()
        or 0
    )

    return jsonify(
        {
            "items": [_serialize_payment(p) for p in items],
            "total": total,
            "stats": {
                "month_count": month_count,
                "month_amount_rub": month_amount,
            },
        }
    )


@bp.route("/bot/settings/rotate-bot-service-token", methods=["POST"])
@token_required
def rotate_bot_service_token():
    new_token = secrets.token_urlsafe(32)
    setting = SystemSetting.query.filter_by(key="bot_service_token").first()
    if setting is None:
        setting = SystemSetting(key="bot_service_token")
        db.session.add(setting)
    setting.value = new_token
    db.session.commit()
    return jsonify({"token": new_token})


_SETTINGS_KEYS = (
    "yookassa_shop_id",
    "yookassa_secret_key",
    "yookassa_return_url",
    "bot_token",
    "admin_ids",
    "telegram_proxy_url",
    "display_timezone",
)
_SECRET_SETTINGS_KEYS = {"yookassa_secret_key", "bot_token"}


def _read_setting(key: str) -> str:
    row = SystemSetting.query.filter_by(key=key).first()
    return row.value if row and row.value else ""


def _parse_admin_ids(raw: str) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for piece in raw.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(int(piece))
        except ValueError:
            continue
    return out


def _normalize_admin_ids_for_storage(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return ",".join(str(i) for i in _parse_admin_ids(value))
    if isinstance(value, list):
        parsed: list[int] = []
        for item in value:
            try:
                parsed.append(int(item))
            except (TypeError, ValueError):
                continue
        return ",".join(str(i) for i in parsed)
    return ""


def _bump_bot_config_version() -> int:
    setting = SystemSetting.query.filter_by(key="bot_config_version").first()
    if setting is None:
        setting = SystemSetting(key="bot_config_version", value="1")
        db.session.add(setting)
        return 1
    try:
        next_v = int(setting.value or "0") + 1
    except ValueError:
        next_v = 1
    setting.value = str(next_v)
    return next_v


@bp.route("/bot/settings", methods=["GET"])
@token_required
def get_bot_settings():
    yookassa_secret = _read_setting("yookassa_secret_key")
    bot_token = _read_setting("bot_token")
    bot_service_token = _read_setting("bot_service_token")
    return jsonify(
        {
            "yookassa_shop_id": _read_setting("yookassa_shop_id"),
            "yookassa_return_url": _read_setting("yookassa_return_url"),
            "yookassa_secret_key": yookassa_secret,
            "bot_token": bot_token,
            "bot_service_token": bot_service_token,
            "has_yookassa_secret": bool(yookassa_secret),
            "has_bot_service_token": bool(bot_service_token),
            "has_bot_token": bool(bot_token),
            "admin_ids": _parse_admin_ids(_read_setting("admin_ids")),
            "telegram_proxy_url": _read_setting("telegram_proxy_url"),
            "display_timezone": _read_setting("display_timezone") or "Europe/Moscow",
            "device_limit_enabled": _read_setting("device_limit_enabled") == "true",
            "device_limit_per_user": int(_read_setting("device_limit_per_user") or "0"),
            "bot_config_version": int(_read_setting("bot_config_version") or "0"),
        }
    )


@bp.route("/bot/settings", methods=["PUT"])
@token_required
def update_bot_settings():
    payload = request.get_json(silent=True) or {}
    changed = False
    for key in _SETTINGS_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if key in _SECRET_SETTINGS_KEYS and not value:
            # don't overwrite secret with empty string — explicit clear uses a separate flag
            continue
        if key == "admin_ids":
            value = _normalize_admin_ids_for_storage(value)
        if key == "display_timezone":
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(str(value or "Europe/Moscow"))
            except Exception:
                return jsonify({"error": f"invalid display_timezone: {value!r}"}), 400
        setting = SystemSetting.query.filter_by(key=key).first()
        new_value = str(value or "")
        if setting is None:
            setting = SystemSetting(key=key, value=new_value)
            db.session.add(setting)
            changed = True
        elif setting.value != new_value:
            setting.value = new_value
            changed = True

    def _upsert(key, new_value):
        nonlocal changed
        row = SystemSetting.query.filter_by(key=key).first()
        if row is None:
            db.session.add(SystemSetting(key=key, value=new_value))
            changed = True
        elif row.value != new_value:
            row.value = new_value
            changed = True

    if "device_limit_enabled" in payload:
        _upsert(
            "device_limit_enabled",
            "true" if payload["device_limit_enabled"] in (True, "true", "True", 1, "1") else "false",
        )
    if "device_limit_per_user" in payload:
        try:
            n = int(payload["device_limit_per_user"])
            if n < 0:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "device_limit_per_user must be a non-negative integer"}), 400
        _upsert("device_limit_per_user", str(n))

    if changed:
        new_version = _bump_bot_config_version()
    else:
        new_version = int(_read_setting("bot_config_version") or "0")
    db.session.commit()
    if changed:
        try:
            bot_events.publish("config_changed", None, {"version": new_version})
        except Exception:
            pass
    return jsonify({"ok": True, "bot_config_version": new_version})
