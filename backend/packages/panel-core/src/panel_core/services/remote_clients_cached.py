import logging

from panel_core.models import LinkedPanel
from panel_core.services.panel_proxy import get_panel_snapshot
from panel_core.services.remote_clients import _bucket_panel_clients

logger = logging.getLogger(__name__)


def remote_clients_by_telegram_id() -> dict[int, list[dict]]:

    bucket: dict[int, list[dict]] = {}
    for panel in LinkedPanel.query.filter_by(enable=True).all():
        snapshot = get_panel_snapshot(panel.id)
        if not snapshot:
            continue
        _bucket_panel_clients(bucket, snapshot, panel)
    return bucket
