import logging

from panel_core.xray.facade import has_local_xray

logger = logging.getLogger(__name__)


def undeliverable_items(tariff):

    if tariff is None or has_local_xray():
        return []
    return [item for item in tariff.items if item.panel_id is None]


def is_deliverable(tariff):

    if tariff is None or not tariff.items:
        return False
    return not undeliverable_items(tariff)


def log_undeliverable(tariff, where):

    if tariff is None:
        return
    if not tariff.items:
        logger.warning(
            "%s: tariff %r (id=%s) carries no items at all, so granting it would produce no key. "
            "Add at least one item in Bot -> Tariffs.",
            where,
            tariff.name,
            tariff.id,
        )
        return
    orphans = undeliverable_items(tariff)
    if not orphans:
        return
    tags = ", ".join(sorted(repr(item.inbound_tag) for item in orphans))
    logger.warning(
        "%s: tariff %r (id=%s) has item(s) on inbound(s) %s with no panel_id. This role runs no local "
        "Xray, so those items point at no node and the grant would fail. Set a panel_id on each item "
        "in Bot -> Tariffs.",
        where,
        tariff.name,
        tariff.id,
        tags,
    )
