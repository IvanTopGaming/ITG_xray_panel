import logging

from panel_core.models import LinkedPanel
from panel_core.services.panel_proxy import fetch_panel_snapshot_live

logger = logging.getLogger(__name__)


def _bucket_panel_clients(bucket: dict[int, list[dict]], snapshot: dict, panel) -> None:

    for ib_data in snapshot.get("inbounds", []):
        inbound_tag = ib_data.get("tag", "")
        inbound_label = ib_data.get("label") or inbound_tag
        for c in ib_data.get("clients", []):
            tg_id = c.get("telegram_id")
            if not tg_id:
                continue
            bucket.setdefault(tg_id, []).append(
                {
                    "id": c.get("id", ""),
                    "email": c.get("email", ""),
                    "inbound_tag": inbound_tag,
                    "inbound_label": inbound_label,
                    "limit_bytes": c.get("limit_bytes", 0),
                    "expiry_time": c.get("expiry_time", 0),
                    "up": c.get("up", 0),
                    "down": c.get("down", 0),
                    "enable": bool(c.get("enable", True)),
                    "reset_day": c.get("reset_day", 0),
                    "last_reset_time": 0,
                    "last_seen": c.get("last_seen", 0),
                    "source_ips": [],
                    "flow": c.get("flow", ""),
                    "preferred_outbound": "",
                    "device_limit": None,
                    "telegram_id": tg_id,
                    "tariff_id": c.get("tariff_id"),
                    "panel_id": panel.id,
                    "panel_name": panel.name,
                }
            )


def remote_clients_by_telegram_id_live(
    panel_ids: set[int] | None = None,
) -> tuple[dict[int, list[dict]], list[dict]]:

    bucket: dict[int, list[dict]] = {}
    unreachable: list[dict] = []
    query = LinkedPanel.query.filter_by(enable=True)
    if panel_ids is not None:
        if not panel_ids:
            return bucket, unreachable
        query = query.filter(LinkedPanel.id.in_(panel_ids))
    for panel in query.all():
        try:
            snapshot = fetch_panel_snapshot_live(panel.id)
        except Exception as exc:
            logger.warning("remote enumerate (live) failed for panel=%s: %s", panel.id, exc)
            unreachable.append({"panel_id": panel.id, "panel_name": panel.name, "error": str(exc)})
            continue
        _bucket_panel_clients(bucket, snapshot, panel)
    return bucket, unreachable
