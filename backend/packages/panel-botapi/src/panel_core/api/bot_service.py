from datetime import datetime
import time
import uuid

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import update

from panel_core.extensions import db
from panel_core.models import (
    BotText,
    Client,
    Inbound,
    LinkedPanel,
    Payment,
    SystemSetting,
    Tariff,
    TelegramUser,
    UserTariffAccess,
)
from panel_core.services import bot_events, tariff_delivery
from panel_core.services.bot_status import record_bot_username, record_bot_version
from panel_core.services.open_access import has_open_ended_access
from panel_core.utils import bot_service_token_required

bp = Blueprint("bot_service", __name__)


def _setting(key: str) -> str:
    row = SystemSetting.query.filter_by(key=key).first()
    return row.value if row and row.value else ""


def _parse_admin_ids_csv(raw: str) -> list[int]:
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


@bp.route("/bot/runtime-config", methods=["GET"])
@bot_service_token_required
def get_runtime_config():

    record_bot_version(request.headers.get("X-Bot-Version"))
    record_bot_username(request.headers.get("X-Bot-Username"))
    return jsonify(
        {
            "version": int(_setting("bot_config_version") or "0"),
            "bot_token": _setting("bot_token"),
            "admin_ids": _parse_admin_ids_csv(_setting("admin_ids")),
            "telegram_proxy_url": _setting("telegram_proxy_url"),
            "display_timezone": _setting("display_timezone") or "Europe/Moscow",
        }
    )


_VALID_LANGS = frozenset({"ru", "en"})


@bp.route("/bot-service/texts", methods=["GET"])
@bot_service_token_required
def get_texts():
    lang = request.args.get("lang", "")
    if lang not in _VALID_LANGS:
        return (
            jsonify({"error": f"lang must be one of {sorted(_VALID_LANGS)}"}),
            400,
        )

    rows = BotText.query.filter_by(lang=lang).all()
    from panel_core.services.bot_texts import text_defaults, validate_bot_text

    defaults = text_defaults()
    texts = {}
    for row in rows:
        try:
            validate_bot_text(row.key, row.text, defaults)
        except ValueError:
            fallback = (defaults.get(row.key) or {}).get(lang)
            if fallback is not None:
                texts[row.key] = fallback
        else:
            texts[row.key] = row.text

    if rows:
        latest = max((r.updated_at for r in rows if r.updated_at is not None), default=None)
        version = int(latest.timestamp()) if latest else 0
    else:
        version = 0
    return jsonify({"version": version, "texts": texts})


def _normalize_language_code(code):
    if not isinstance(code, str):
        return "ru"
    code = code.lower().strip()
    if code.startswith("ru"):
        return "ru"
    if code.startswith("en"):
        return "en"
    return "ru"


def _serialize_telegram_user(u):
    return {
        "telegram_id": u.telegram_id,
        "username": u.username,
        "language": u.language,
        "trial_used_at": u.trial_used_at.isoformat() if u.trial_used_at else None,
        "blocked": u.blocked,
        "language_chosen": u.language_chosen,
        "first_seen_at": u.first_seen_at.isoformat() if u.first_seen_at else None,
        "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
    }


@bp.route("/bot-service/users", methods=["POST"])
@bot_service_token_required
def upsert_user():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected JSON object"}), 400

    tg_id = payload.get("telegram_id")
    if not isinstance(tg_id, int) or isinstance(tg_id, bool):
        return jsonify({"error": "telegram_id (integer) is required"}), 400

    username = payload.get("username") or None
    if username is not None and not isinstance(username, str):
        return jsonify({"error": "username must be a string or null"}), 400

    language_code = payload.get("language_code")
    detected_lang = _normalize_language_code(language_code)

    user = db.session.get(TelegramUser, tg_id)
    if user is None:
        user = TelegramUser(
            telegram_id=tg_id,
            username=username,
            language=detected_lang,
        )
        db.session.add(user)
    else:
        user.username = username
        user.last_seen_at = datetime.utcnow()
    if not getattr(user, "sub_token", None):
        user.sub_token = str(uuid.uuid4())
    db.session.commit()
    return jsonify(_serialize_telegram_user(user))


def _deliverable_trial_tariff():

    for candidate in Tariff.query.filter_by(is_trial=True, enabled=True).all():
        if tariff_delivery.is_deliverable(candidate):
            return candidate
        tariff_delivery.log_undeliverable(candidate, "bot_service.trial")
    return None


@bp.route("/bot-service/trial/activate", methods=["POST"])
@bot_service_token_required
def activate_trial():
    from panel_core.models import ProvisionOperation
    from panel_core.services.provisioning_operations import queue_grant, run_operation

    payload = request.get_json(silent=True) or {}
    tg_id = payload.get("telegram_id")
    if not isinstance(tg_id, int) or isinstance(tg_id, bool):
        return jsonify({"error": "telegram_id (integer) is required"}), 400

    user = db.session.get(TelegramUser, tg_id)
    if user is None:
        user = TelegramUser(telegram_id=tg_id, language="ru")
        db.session.add(user)
        db.session.flush()

    if user.blocked:
        return jsonify({"error": "account_blocked"}), 403
    if user.trial_used_at is not None:
        return jsonify({"error": "trial already used"}), 409

    if user.trial_operation_id:
        operation = db.session.get(ProvisionOperation, user.trial_operation_id)
        if operation is None:
            return jsonify({"error": "trial_operation_requires_review"}), 409
        result = run_operation(operation)
        return jsonify(result), 202 if result["panel_failures"] else 200

    if has_open_ended_access(tg_id):
        return jsonify({"error": "open_ended_access"}), 409

    trial_tariff = _deliverable_trial_tariff()
    if trial_tariff is None:
        return jsonify({"error": "no trial tariff configured"}), 404

    claimed = db.session.execute(
        update(TelegramUser)
        .where(
            TelegramUser.telegram_id == tg_id,
            TelegramUser.trial_used_at.is_(None),
            TelegramUser.trial_operation_id.is_(None),
        )
        .values(trial_operation_id=f"trial:{tg_id}:{trial_tariff.id}")
    )
    if claimed.rowcount == 0:
        db.session.rollback()
        return jsonify({"error": "trial already used"}), 409
    operation = queue_grant(tg_id, trial_tariff, source="trial", operation_id=f"trial:{tg_id}:{trial_tariff.id}")
    result = run_operation(operation)
    return jsonify(result), 202 if result["panel_failures"] else 200


@bp.route("/bot-service/users/<int:tg_id>/state", methods=["GET"])
@bot_service_token_required
def get_user_state(tg_id):

    from panel_core.services.subscription_sources import (
        SubscriptionUnavailable,
        access_reason,
        remote_subscription_clients,
    )
    from panel_core.services.share_links import build_remote_link

    user = db.session.get(TelegramUser, tg_id)
    blocked = bool(user and user.blocked)
    open_ended = has_open_ended_access(tg_id)
    trial_tariff = _deliverable_trial_tariff() if not blocked and not open_ended else None
    trial_available = (user is None or user.trial_used_at is None) and trial_tariff is not None

    clients = (
        []
        if blocked
        else [client for client in Client.query.filter_by(telegram_id=tg_id).all() if access_reason(client) == "active"]
    )
    clients_data = [{**c.to_dict(), "links": []} for c in clients]
    try:
        pairs = [] if blocked else remote_subscription_clients(telegram_id=tg_id)
        panel_names = {} if blocked else dict(db.session.query(LinkedPanel.id, LinkedPanel.name).all())
        for panel_host, ib_data, remote_client, stream in pairs:
            links = build_remote_link(panel_host, {**ib_data, "stream_settings": stream}, remote_client)
            if not links:
                raise SubscriptionUnavailable("Active protocol has no bot delivery representation")
            clients_data.append(
                {
                    **remote_client,
                    "inbound_tag": ib_data.get("tag", ""),
                    "inbound_label": ib_data.get("label") or ib_data.get("tag", ""),
                    "panel_id": ib_data["panel_id"],
                    "panel_name": panel_names.get(ib_data["panel_id"], ""),
                    "links": links,
                }
            )
    except (SubscriptionUnavailable, TypeError, ValueError):
        current_app.logger.exception("bot user state unavailable: telegram_id=%s", tg_id)
        return jsonify({"error": "subscription_unavailable"}), 503, {"Cache-Control": "no-store", "Retry-After": "30"}

    if clients_data:
        from panel_core.services.expiry import nearest_expiry

        expires_at_ms = nearest_expiry(
            [c.get("expiry_time") for c in clients_data],
            fallback=None,
        )
    else:
        expires_at_ms = None

    from panel_core.services.sub_links import build_aggregate_sub_url

    sub_url = build_aggregate_sub_url(user.sub_token) if user else None

    return jsonify(
        {
            "telegram_id": tg_id,
            "language": user.language if user else "ru",
            "language_chosen": user.language_chosen if user else False,
            "open_ended_access": open_ended,
            "trial_available": trial_available,
            "trial_days": trial_tariff.period_days if trial_tariff is not None else None,
            "trial_used_at": user.trial_used_at.isoformat() if user and user.trial_used_at else None,
            "blocked": user.blocked if user else False,
            "clients": clients_data,
            "expires_at_ms": expires_at_ms,
            "sub_url": sub_url,
        }
    )


@bp.route("/bot-service/users/<int:tg_id>/language", methods=["POST"])
@bot_service_token_required
def set_user_language(tg_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected JSON object"}), 400

    lang = payload.get("language")
    if lang not in _VALID_LANGS:
        return jsonify({"error": f"language must be one of {sorted(_VALID_LANGS)}"}), 400

    user = db.session.get(TelegramUser, tg_id)
    if user is None:
        return jsonify({"error": "user not found"}), 404

    user.language = lang
    user.language_chosen = True
    db.session.commit()

    bot_events.publish("user_language_changed", telegram_id=tg_id, payload={"language": lang})

    return jsonify(
        {
            "telegram_id": tg_id,
            "language": user.language,
            "language_chosen": user.language_chosen,
        }
    )


@bp.route("/bot-service/tariffs", methods=["GET"])
@bot_service_token_required
def list_tariffs_for_bot():
    tg_id_raw = request.args.get("for")
    try:
        telegram_id = int(tg_id_raw) if tg_id_raw else None
    except ValueError:
        telegram_id = None

    if has_open_ended_access(telegram_id):
        return jsonify([])

    public = Tariff.query.filter(
        Tariff.enabled.is_(True),
        Tariff.is_trial.is_(False),
        Tariff.visibility == "public",
    ).all()
    private = []
    if telegram_id is not None:
        granted_ids = [access.tariff_id for access in UserTariffAccess.query.filter_by(telegram_id=telegram_id).all()]
        if granted_ids:
            private = Tariff.query.filter(
                Tariff.id.in_(granted_ids),
                Tariff.enabled.is_(True),
                Tariff.is_trial.is_(False),
                Tariff.visibility != "archived",
            ).all()

    seen, ordered = set(), []
    for t in public + private:
        if t.id in seen:
            continue
        seen.add(t.id)
        if not tariff_delivery.is_deliverable(t):
            tariff_delivery.log_undeliverable(t, "bot_service.list_tariffs")
            continue
        ordered.append(t)
    ordered.sort(key=lambda t: (t.sort_order or 0, t.id))

    active_tariff_ids: set[int] = set()
    if telegram_id is not None:
        now_ms = int(time.time() * 1000)
        rows = (
            db.session.query(Client.tariff_id)
            .filter(
                Client.telegram_id == telegram_id,
                Client.tariff_id.isnot(None),
                Client.enable.is_(True),
                db.or_(Client.expiry_time == 0, Client.expiry_time > now_ms),
            )
            .distinct()
            .all()
        )
        active_tariff_ids = {r[0] for r in rows if r[0] is not None}

    from panel_core.services.panel_proxy import get_panel_snapshot
    from panel_core.services.subscription_sources import access_reason

    inbound_labels: dict[tuple[int | None, str], str | None] = {
        (None, tag): label for tag, label in db.session.query(Inbound.tag, Inbound.label).all()
    }
    for panel in LinkedPanel.query.filter_by(enable=True).all():
        snapshot = get_panel_snapshot(panel.id)
        if not snapshot:
            continue
        for ib_data in snapshot.get("inbounds", []):
            tag = ib_data.get("tag")
            if not tag:
                continue
            inbound_labels[(panel.id, tag)] = ib_data.get("label")
            if telegram_id is not None:
                for remote_client in ib_data.get("clients", []):
                    if (
                        remote_client.get("telegram_id") == telegram_id
                        and remote_client.get("tariff_id") is not None
                        and remote_client.get("expiry_time") is not None
                        and access_reason(remote_client) == "active"
                    ):
                        active_tariff_ids.add(remote_client["tariff_id"])

    return jsonify(
        [_serialize_tariff_for_bot(t, active_ids=active_tariff_ids, inbound_labels=inbound_labels) for t in ordered]
    )


@bp.route("/bot-service/payments/<int:payment_id>/cancel", methods=["POST"])
@bot_service_token_required
def cancel_payment_for_bot(payment_id):
    payload = request.get_json(silent=True) or {}
    tg_id = payload.get("telegram_id")
    if not isinstance(tg_id, int) or isinstance(tg_id, bool):
        return jsonify({"error": "telegram_id (integer) is required"}), 400
    p = Payment.query.filter_by(id=payment_id, telegram_id=tg_id).first()
    if p is None:
        return jsonify({"error": "not_found"}), 404
    from panel_core.services import billing

    db.session.execute(
        update(Payment)
        .where(
            Payment.id == p.id,
            Payment.telegram_id == tg_id,
            Payment.status == "pending",
            Payment.provider_status.in_(("pending", "waiting_for_capture")),
            Payment.fulfillment_status == "pending",
            Payment.cancel_requested_at.is_(None),
        )
        .values(cancel_requested_at=billing._now())
    )
    db.session.commit()
    db.session.refresh(p)
    ui_closed = (
        p.status == "pending"
        and p.provider_status in ("pending", "waiting_for_capture")
        and p.fulfillment_status == "pending"
        and p.cancel_requested_at is not None
    )
    return jsonify({"id": p.id, "status": p.status, "ui_closed": ui_closed, **billing.payment_state(p)})


@bp.route("/bot-service/payments/<int:payment_id>/chat-coords", methods=["POST"])
@bot_service_token_required
def set_payment_chat_coords(payment_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected JSON object"}), 400

    chat_id = payload.get("chat_id")
    message_id = payload.get("message_id")
    tg_id = payload.get("telegram_id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return jsonify({"error": "chat_id (integer) is required"}), 400
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        return jsonify({"error": "message_id (integer) is required"}), 400
    if not isinstance(tg_id, int) or isinstance(tg_id, bool):
        return jsonify({"error": "telegram_id (integer) is required"}), 400

    payment = Payment.query.filter_by(id=payment_id, telegram_id=tg_id).first()
    if payment is None:
        return jsonify({"error": "payment not found"}), 404

    payment.chat_id = chat_id
    payment.message_id = message_id
    db.session.commit()

    return jsonify(
        {
            "payment_id": payment.id,
            "chat_id": payment.chat_id,
            "message_id": payment.message_id,
        }
    )


def _serialize_tariff_for_bot(t, active_ids=frozenset(), inbound_labels=None):
    labels = inbound_labels or {}
    return {
        "id": t.id,
        "name": t.name,
        "price_rub": t.price_rub,
        "period_days": t.period_days,
        "is_active": t.id in active_ids,
        "items": [
            {
                "inbound_tag": i.inbound_tag,
                "label": i.label or "",
                "inbound_label": labels.get((i.panel_id, i.inbound_tag)) or i.inbound_tag,
                "panel_id": i.panel_id,
                "traffic_gb": i.traffic_gb,
            }
            for i in t.items
        ],
    }


@bp.route("/bot-service/notifications/claim", methods=["POST"])
@bot_service_token_required
def claim_notification_endpoint():

    data = request.get_json(silent=True) or {}
    telegram_id = data.get("telegram_id")
    kind = data.get("kind")

    if telegram_id is None or not kind:
        return jsonify({"error": "telegram_id and kind are required"}), 400
    if not isinstance(data.get("scope", ""), str) or len(data.get("scope", "")) > 200:
        return jsonify({"error": "scope must be at most 200 characters"}), 400

    from panel_core.services.notifications import claim_notification

    try:
        result = claim_notification(
            telegram_id=int(telegram_id),
            kind=str(kind),
            tariff_id=data.get("tariff_id"),
            scope=str(data.get("scope") or ""),
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        current_app.logger.exception("notification claim failed")
        return jsonify({"error": "internal server error"}), 500

    return jsonify(result), 200
