from panel_core.models import UserTariffAccess


def has_open_ended_access(telegram_id) -> bool:
    if telegram_id is None:
        return False
    return (
        UserTariffAccess.query.filter(
            UserTariffAccess.telegram_id == telegram_id,
            UserTariffAccess.billing == "free",
            UserTariffAccess.access_until.is_(None),
        ).first()
        is not None
    )
