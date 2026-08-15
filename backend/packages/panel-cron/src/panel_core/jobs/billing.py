import logging
from datetime import datetime, timedelta

from panel_core.extensions import db
from panel_core.models import Tariff, UserTariffAccess
from panel_core.services.panel_proxy import proxy_bulk_reset_traffic
from panel_core.services.remote_clients_cached import remote_clients_by_telegram_id

logger = logging.getLogger(__name__)


def reset_grant_traffic_cycles() -> None:

    now = datetime.utcnow()
    due = (
        UserTariffAccess.query.filter(UserTariffAccess.billing == "free")
        .filter(UserTariffAccess.next_renewal_at.isnot(None))
        .filter(UserTariffAccess.next_renewal_at <= now)
        .filter((UserTariffAccess.access_until.is_(None)) | (UserTariffAccess.access_until > now))
        .limit(500)
        .all()
    )
    if not due:
        return

    by_telegram_id = remote_clients_by_telegram_id()
    reset_by_panel: dict[int, dict[tuple[str, str], dict]] = {}

    for grant in due:
        tariff = db.session.get(Tariff, grant.tariff_id)
        if tariff is None:
            grant.next_renewal_at = None
            db.session.commit()
            logger.info("traffic reset: grant %s names no tariff, unscheduled", grant.id)
            continue

        owned = {(item.panel_id, item.inbound_tag) for item in tariff.items if item.panel_id is not None}
        per_panel: dict[int, list[dict]] = {}
        for record in by_telegram_id.get(grant.telegram_id, []):
            if (
                record.get("tariff_id") == grant.tariff_id
                and (record.get("panel_id"), record.get("inbound_tag")) in owned
            ):
                per_panel.setdefault(record["panel_id"], []).append(
                    {"tag": record["inbound_tag"], "email": record["email"], "reenable": True}
                )

        for panel_id, users in per_panel.items():
            bucket = reset_by_panel.setdefault(panel_id, {})
            for user in users:
                bucket[(user["tag"], user["email"])] = user

        grant.next_renewal_at = now + timedelta(days=tariff.period_days)
        db.session.commit()
        logger.info(
            "traffic reset: tg=%s tariff=%s panels=%d next=%s",
            grant.telegram_id,
            grant.tariff_id,
            len(per_panel),
            grant.next_renewal_at,
        )

    for panel_id, users_by_key in reset_by_panel.items():
        try:
            proxy_bulk_reset_traffic(panel_id, list(users_by_key.values()))
        except Exception as exc:
            logger.error("traffic reset failed for panel=%s: %s", panel_id, exc)
