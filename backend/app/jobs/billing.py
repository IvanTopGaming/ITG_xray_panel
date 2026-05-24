"""Auto-renewal cron for free-tier grants."""

import logging
from datetime import datetime, timedelta

from app.extensions import db
from app.models import Tariff, TelegramUser, UserTariffAccess
from app.services import bot_events
from app.services.provisioning import apply_tariff_for_user

logger = logging.getLogger(__name__)


def auto_renew_free_users() -> None:
    """Re-provision + bump each due free grant. Pauses (with notification) when tariff is gone/archived/disabled."""
    now = datetime.utcnow()
    due = (
        UserTariffAccess.query.filter(UserTariffAccess.billing == "free")
        .filter(UserTariffAccess.next_renewal_at <= now)
        .limit(500)
        .all()
    )
    for grant in due:
        tariff = Tariff.query.get(grant.tariff_id)
        if tariff is None or tariff.visibility == "archived" or not tariff.enabled:
            grant.next_renewal_at = None
            db.session.commit()
            reason = "missing" if tariff is None else "archived" if tariff.visibility == "archived" else "disabled"
            logger.info(
                "auto_renew: pausing tg=%s tariff=%s (%s)",
                grant.telegram_id,
                grant.tariff_id,
                reason,
            )
            user = TelegramUser.query.get(grant.telegram_id)
            tariff_name = tariff.name if tariff is not None else f"#{grant.tariff_id}"
            try:
                bot_events.publish(
                    "access_paused",
                    telegram_id=grant.telegram_id,
                    payload={
                        "tariff_id": grant.tariff_id,
                        "tariff_name": tariff_name,
                        "reason": reason,
                        "lang": (user.language if user else "ru"),
                    },
                )
            except Exception as exc:
                logger.warning("auto_renew: access_paused publish failed: %s", exc)
            continue
        try:
            result = apply_tariff_for_user(grant.telegram_id, tariff, source="auto_renew")
            grant.next_renewal_at = now + timedelta(days=tariff.period_days)
            db.session.commit()
            user = TelegramUser.query.get(grant.telegram_id)
            bot_events.publish(
                "access_renewed",
                telegram_id=grant.telegram_id,
                payload={
                    "tariff_id": tariff.id,
                    "tariff_name": tariff.name,
                    "expires_at_ms": result["expires_at_ms"],
                    "lang": (user.language if user else "ru"),
                },
            )
        except Exception as exc:
            db.session.rollback()
            logger.error(
                "auto_renew failed for tg=%s tariff=%s: %s",
                grant.telegram_id,
                grant.tariff_id,
                exc,
            )
