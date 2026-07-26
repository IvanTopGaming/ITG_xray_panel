import logging
import os
import secrets
import time
from datetime import datetime, timedelta

import yaml
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

from panel_core.extensions import db
from panel_core.models import (
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
from panel_core.services import bot_events
from panel_core.services.panel_proxy import get_panel_snapshot, proxy_update_user
from panel_core.services.provisioning import apply_tariff_for_user, backfill_tariff
from panel_core.services.remote_clients import (
    _bucket_panel_clients,
    remote_clients_by_telegram_id_live as _remote_clients_by_telegram_id_live,
)
from panel_core.xray.facade import (
    generate_config_file,
    restart_xray_container,
    _api_add_user_grpc,
    _api_remove_user_grpc,
)
from panel_core.utils import token_required

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

    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if len(name) > 120:
        raise ValueError("name too long (max 120)")
    price = payload.get("price_rub")
    if not isinstance(price, int) or price < 0 or price > 10_000_000:
        raise ValueError("price_rub must be between 0 and 10000000")
    period = payload.get("period_days")
    if not isinstance(period, int) or period <= 0 or period > 3650:
        raise ValueError("period_days must be between 1 and 3650")
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
        panel_id = item.get("panel_id")
        if not isinstance(panel_id, int) or isinstance(panel_id, bool) or panel_id <= 0:
            raise ValueError(
                f"items[{i}].panel_id is required: this panel does not run Xray itself, so the item "
                f"for inbound {tag!r} must name the node that will serve it. Pick a linked panel for this item."
            )


def _apply_items(tariff, items_payload):

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

    orphan_tags = sorted(item.inbound_tag for item in src.items if item.panel_id is None)
    if orphan_tags:
        return (
            jsonify(
                {
                    "error": (
                        f"tariff {src.name!r} still has item(s) without panel_id ({', '.join(orphan_tags)}); "
                        "copying them would create another tariff that provisions nothing. "
                        "Set a panel_id on the source tariff first."
                    )
                }
            ),
            400,
        )

    copy = Tariff(
        name=f"{src.name} (копия)",
        price_rub=src.price_rub,
        period_days=src.period_days,
        visibility="public",
        is_trial=False,
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
                    "customized": bool(r.customized),
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]
        }
    )


@bp.route("/bot/texts/keys", methods=["GET"])
@token_required
def list_text_keys():

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
        row = BotText(key=key, lang=lang, text=text, customized=True)
        db.session.add(row)
    else:
        row.text = text

        row.customized = True
    db.session.commit()

    bot_events.publish("texts_changed", telegram_id=None, payload={"lang": lang})

    return jsonify(
        {
            "key": row.key,
            "lang": row.lang,
            "text": row.text,
            "customized": bool(row.customized),
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

    bucket: dict[int, list[dict]] = {}
    for panel in LinkedPanel.query.filter_by(enable=True).all():
        snapshot = get_panel_snapshot(panel.id)
        if not snapshot:
            continue
        _bucket_panel_clients(bucket, snapshot, panel)
    return bucket


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

    user = db.session.get(TelegramUser, tg_id)
    if user is None:
        return jsonify({"error": "user not found"}), 404

    active_clients = Client.query.filter_by(telegram_id=tg_id, enable=True).all()

    inbound_tags = {c.inbound_tag for c in active_clients}
    inbounds_by_tag = (
        {ib.tag: ib for ib in Inbound.query.filter(Inbound.tag.in_(inbound_tags)).all()} if inbound_tags else {}
    )

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

    active_clients = Client.query.filter_by(telegram_id=tg_id, tariff_id=tariff_id, enable=True).all()
    inbound_tags = {c.inbound_tag for c in active_clients}
    inbounds_by_tag = (
        {ib.tag: ib for ib in Inbound.query.filter(Inbound.tag.in_(inbound_tags)).all()} if inbound_tags else {}
    )

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
        panel_ids = {pid for pid, _tag in remote_items}
        remote_by_tg, unreachable = _remote_clients_by_telegram_id_live(panel_ids=panel_ids)
        panel_failures.extend(unreachable)
        for rc in remote_by_tg.get(tg_id, []):
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
