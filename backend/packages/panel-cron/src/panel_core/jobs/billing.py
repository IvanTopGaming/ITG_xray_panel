import logging
from datetime import datetime, timedelta

from sqlalchemy import update

from panel_core.extensions import db
from panel_core.models import ProvisionOperation, Tariff, UserTariffAccess
from panel_core.services.provisioning_operations import queue_operation, run_operation, source_targets

logger = logging.getLogger(__name__)


def reset_grant_traffic_cycles() -> None:

    now = datetime.utcnow()
    due = (
        UserTariffAccess.query.filter(UserTariffAccess.billing == "free")
        .filter(UserTariffAccess.next_renewal_at.isnot(None))
        .filter(UserTariffAccess.next_renewal_at <= now)
        .filter((UserTariffAccess.access_until.is_(None)) | (UserTariffAccess.access_until > now))
        .order_by(UserTariffAccess.next_renewal_at, UserTariffAccess.id)
        .all()
    )
    if not due:
        return

    for grant in due:
        original_due = grant.next_renewal_at
        revision = grant.provisioning_revision
        try:
            tariff = db.session.get(Tariff, grant.tariff_id)
            if tariff is None or not tariff.items or tariff.period_days <= 0:
                raise ValueError("Traffic cycle has no valid tariff targets or period")
            if not any((item.traffic_gb or 0) > 0 for item in tariff.items):
                grant.next_renewal_at = None
                db.session.commit()
                continue
            source_id = f"grant:{grant.id}"
            targets = source_targets(
                source_id,
                {
                    "items": [
                        {"panel_id": item.panel_id, "inbound_tag": item.inbound_tag}
                        for item in tariff.items
                        if (item.traffic_gb or 0) > 0
                    ]
                },
            )
            operation_id = f"cycle:{grant.id}:{original_due.isoformat()}"
            operation = db.session.get(ProvisionOperation, operation_id) or queue_operation(
                operation_id=operation_id,
                telegram_id=grant.telegram_id,
                tariff_id=grant.tariff_id,
                source="grant_cycle",
                source_id=source_id,
                source_revision=revision,
                snapshot={"items": targets},
                params={"cycle_id": operation_id},
                kind="reset",
            )
            result = run_operation(operation)
            if result.get("status") != "succeeded" or result.get("panel_failures"):
                logger.error("grant traffic cycle pending: grant_id=%s operation_id=%s", grant.id, operation_id)
                continue
            period = timedelta(days=tariff.period_days)
            next_due = original_due + period * ((now - original_due) // period + 1)
            db.session.execute(
                update(UserTariffAccess)
                .where(
                    UserTariffAccess.id == grant.id,
                    UserTariffAccess.next_renewal_at == original_due,
                    UserTariffAccess.provisioning_revision == revision,
                    UserTariffAccess.billing == "free",
                )
                .values(next_renewal_at=next_due)
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("grant traffic cycle failed: grant_id=%s", grant.id)
