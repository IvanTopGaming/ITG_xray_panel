from panel_core.extensions import db
from panel_core.models import Inbound, LinkedPanel
from panel_core.services.panel_proxy import get_panel_snapshot
from panel_core.xray.facade import has_local_xray


SUPPORTED_PROTOCOLS = {"vless", "vmess", "trojan", "shadowsocks"}


def validate_target(panel_id, inbound_tag):
    if panel_id is None:
        if not has_local_xray():
            raise ValueError(f"inbound {inbound_tag!r} requires panel_id on this role")
        inbound = Inbound.query.filter_by(tag=inbound_tag).first()
        protocol = inbound.protocol if inbound is not None else None
    else:
        panel = db.session.get(LinkedPanel, panel_id)
        if panel is None or not panel.enable:
            raise ValueError(f"panel {panel_id} is missing or disabled")
        snapshot = get_panel_snapshot(panel_id)
        if not snapshot:
            raise ValueError(f"panel {panel_id} target metadata unavailable")
        inbound = next((item for item in snapshot.get("inbounds", []) if item.get("tag") == inbound_tag), None)
        protocol = inbound.get("protocol") if inbound else None
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"inbound {inbound_tag!r} is missing or its protocol has no subscription delivery")


def validate_tariff_targets(tariff):
    if tariff is None or not tariff.items:
        raise ValueError("tariff_has_no_deliverable_items")
    for item in tariff.items:
        validate_target(item.panel_id, item.inbound_tag)
