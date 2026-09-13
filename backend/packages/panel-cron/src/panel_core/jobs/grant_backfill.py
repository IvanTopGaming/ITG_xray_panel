import logging
from datetime import datetime, timedelta

from panel_core.extensions import db
from panel_core.models import SystemSetting, Tariff, UserTariffAccess
from panel_core.services.provisioning import apply_tariff_for_user

logger = logging.getLogger(__name__)

_FLAG = "grants_open_ended_backfill"


def backfill_open_ended_grants() -> int:

    if SystemSetting.query.filter_by(key=_FLAG).first() is not None:
        return 0

    failed = 0
    legacy_gifts = UserTariffAccess.query.filter(
        UserTariffAccess.billing == "gift", UserTariffAccess.legacy_migration_version == 0
    ).all()
    paused = (
        UserTariffAccess.query.filter(UserTariffAccess.billing == "free")
        .filter(UserTariffAccess.next_renewal_at.is_(None))
        .filter(UserTariffAccess.access_until.is_(None))
        .filter(UserTariffAccess.legacy_migration_version == 0)
        .all()
    )
    for grant in legacy_gifts:
        tariff = db.session.get(Tariff, grant.tariff_id)
        if tariff is None:
            failed += 1
            logger.error("legacy gift backfill: missing tariff=%s grant=%s", grant.tariff_id, grant.id)
            continue
        grant.billing = "free"
        grant.access_until = (grant.created_at or datetime.utcnow()) + timedelta(days=tariff.period_days)
        grant.legacy_migration_version = 1
    for grant in paused:
        grant.access_until = grant.created_at or datetime.utcnow()
        grant.legacy_migration_version = 1
    if legacy_gifts or paused:
        db.session.commit()

    live = (
        UserTariffAccess.query.filter(UserTariffAccess.billing == "free")
        .filter(UserTariffAccess.next_renewal_at.isnot(None))
        .filter(UserTariffAccess.legacy_migration_version == 0)
        .all()
    )

    converted = 0
    for grant in live:
        tariff = db.session.get(Tariff, grant.tariff_id)
        if tariff is None:
            failed += 1
            logger.error("open-ended backfill: missing tariff=%s grant=%s", grant.tariff_id, grant.id)
            continue
        limits_traffic = any((item.traffic_gb or 0) > 0 for item in tariff.items)
        try:
            apply_tariff_for_user(
                grant.telegram_id,
                tariff,
                source="grant_backfill",
                operation_id=f"backfill:{grant.id}",
                source_id=f"grant:{grant.id}",
                source_revision=grant.provisioning_revision,
                expiry_ms=0,
            )
        except Exception as exc:
            db.session.rollback()
            failed += 1
            logger.error(
                "open-ended backfill failed for tg=%s tariff=%s: %s",
                grant.telegram_id,
                grant.tariff_id,
                exc,
            )
            continue

        grant.access_until = None
        grant.legacy_migration_version = 1
        if not limits_traffic:
            grant.next_renewal_at = None
        db.session.commit()
        converted += 1
        logger.info("open-ended backfill: tg=%s tariff=%s", grant.telegram_id, grant.tariff_id)

    if failed:
        logger.warning(
            "open-ended backfill: %d grant(s) converted, %d unreachable — retrying on next start-up",
            converted,
            failed,
        )
        return converted

    db.session.add(SystemSetting(key=_FLAG, value="done"))
    db.session.commit()
    logger.info("open-ended backfill complete: %d grant(s) converted", converted)
    return converted
