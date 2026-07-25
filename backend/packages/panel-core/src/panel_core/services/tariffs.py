import logging

from panel_core.extensions import db
from panel_core.models import Tariff, TariffItem

logger = logging.getLogger(__name__)


def purge_tariff_items(*criteria):
    rows = db.session.query(TariffItem.tariff_id).filter(*criteria).all()
    if not rows:
        return {"removed": 0, "disabled_tariffs": []}

    affected_tariff_ids = {tariff_id for (tariff_id,) in rows}
    removed = len(rows)

    TariffItem.query.filter(*criteria).delete(synchronize_session=False)
    db.session.flush()

    disabled_tariffs = []
    for tariff_id in affected_tariff_ids:
        remaining = TariffItem.query.filter_by(tariff_id=tariff_id).count()
        if remaining:
            continue
        tariff = db.session.get(Tariff, tariff_id)
        if tariff is not None and tariff.enabled:
            tariff.enabled = False
            disabled_tariffs.append(tariff_id)

    if disabled_tariffs:
        logger.warning(
            "purge_tariff_items removed %d tariff item(s); disabled now-empty tariff(s): %s",
            removed,
            sorted(disabled_tariffs),
        )
    else:
        logger.info(
            "purge_tariff_items removed %d tariff item(s) from tariff(s) %s",
            removed,
            sorted(affected_tariff_ids),
        )

    return {"removed": removed, "disabled_tariffs": sorted(disabled_tariffs)}
