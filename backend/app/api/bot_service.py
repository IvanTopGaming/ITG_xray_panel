from datetime import datetime
import time
import uuid

from flask import Blueprint, jsonify, request
from sqlalchemy import update

from app.extensions import db
from app.models import (
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
from app.services import bot_events
from app.services.bot_status import record_bot_version
from app.services.provisioning import apply_tariff_for_user
from app.utils import bot_service_token_required

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
    texts = {row.key: row.text for row in rows}

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


@bp.route("/bot-service/trial/activate", methods=["POST"])
@bot_service_token_required
def activate_trial():
    payload = request.get_json(silent=True) or {}
    tg_id = payload.get("telegram_id")
    if not isinstance(tg_id, int) or isinstance(tg_id, bool):
        return jsonify({"error": "telegram_id (integer) is required"}), 400

    user = db.session.get(TelegramUser, tg_id)
    if user is None:
        user = TelegramUser(telegram_id=tg_id, language="ru")
        db.session.add(user)
        db.session.flush()

    if user.trial_used_at is not None:
        return jsonify({"error": "trial already used"}), 409

    trial_tariff = Tariff.query.filter_by(is_trial=True, enabled=True).first()
    if trial_tariff is None:
        return jsonify({"error": "no trial tariff configured"}), 404

    claimed = db.session.execute(
        update(TelegramUser)
        .where(TelegramUser.telegram_id == tg_id, TelegramUser.trial_used_at.is_(None))
        .values(trial_used_at=datetime.utcnow())
    )
    db.session.commit()
    if claimed.rowcount == 0:
        return jsonify({"error": "trial already used"}), 409

    try:
        result = apply_tariff_for_user(tg_id, trial_tariff, source="trial")
    except Exception:
        db.session.rollback()
        db.session.execute(update(TelegramUser).where(TelegramUser.telegram_id == tg_id).values(trial_used_at=None))
        db.session.commit()
        raise

    bot_events.publish(
        "trial_activated",
        telegram_id=tg_id,
        payload={
            "tariff_id": trial_tariff.id,
            "tariff_name": trial_tariff.name,
            "expires_at_ms": result["expires_at_ms"],
        },
    )

    return jsonify(result)


@bp.route("/bot-service/users/<int:tg_id>/state", methods=["GET"])
@bot_service_token_required
def get_user_state(tg_id):

    user = db.session.get(TelegramUser, tg_id)
    trial_available = (user is None or user.trial_used_at is None) and Tariff.query.filter_by(
        is_trial=True, enabled=True
    ).first() is not None

    clients = Client.query.filter_by(telegram_id=tg_id, enable=True).all()
    clients_data = [c.to_dict() for c in clients]

    from app.models import LinkedPanel
    from app.services.panel_proxy import get_panel_snapshot

    for panel in LinkedPanel.query.filter_by(enable=True).all():
        snapshot = get_panel_snapshot(panel.id)
        if not snapshot:
            continue
        for ib_data in snapshot.get("inbounds", []):
            for c in ib_data.get("clients", []):
                if c.get("telegram_id") != tg_id or not c.get("enable", True):
                    continue
                clients_data.append(
                    {
                        **c,
                        "inbound_tag": ib_data.get("tag", ""),
                        "inbound_label": ib_data.get("label") or ib_data.get("tag", ""),
                        "panel_id": panel.id,
                        "panel_name": panel.name,
                    }
                )

    if clients_data:
        expires_at_ms = max(c.get("expiry_time", 0) or 0 for c in clients_data)
    else:
        expires_at_ms = None

    from app.api.subscription import build_aggregate_sub_url

    sub_url = build_aggregate_sub_url(user.sub_token) if user else None

    return jsonify(
        {
            "telegram_id": tg_id,
            "language": user.language if user else "ru",
            "language_chosen": user.language_chosen if user else False,
            "trial_available": trial_available,
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

    from app.services.panel_proxy import get_panel_snapshot

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
    if p.status == "pending":
        p.status = "cancelled"
        db.session.commit()
    return jsonify({"id": p.id, "status": p.status})


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
