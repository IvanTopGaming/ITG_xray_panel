import datetime as dt

from sqlalchemy import and_, or_

from panel_core.models import UserTariffAccess


def eligible_tariff_access(telegram_id):
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    return UserTariffAccess.query.filter(
        UserTariffAccess.telegram_id == telegram_id,
        UserTariffAccess.provisioning_status == "succeeded",
        or_(
            UserTariffAccess.billing == "paid",
            and_(
                UserTariffAccess.billing == "free",
                or_(UserTariffAccess.access_until.is_(None), UserTariffAccess.access_until > now),
            ),
        ),
    )


def has_open_ended_access(telegram_id) -> bool:
    if telegram_id is None:
        return False
    return (
        UserTariffAccess.query.filter(
            UserTariffAccess.telegram_id == telegram_id,
            UserTariffAccess.billing == "free",
            UserTariffAccess.provisioning_status == "succeeded",
            UserTariffAccess.access_until.is_(None),
        ).first()
        is not None
    )
