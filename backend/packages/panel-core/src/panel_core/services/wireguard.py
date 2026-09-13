import hashlib
import ipaddress

from panel_core.extensions import db
from panel_core.models import Client


def _assigned_addresses(clients, *, allow_missing=False):
    result = {}
    used = set()
    for client in clients:
        if not client.wg_address:
            if allow_missing:
                continue
            raise ValueError(f"WireGuard client '{client.email}' has no assigned address")
        try:
            address = ipaddress.ip_interface(client.wg_address)
        except ValueError as exc:
            raise ValueError(f"Invalid WireGuard address for '{client.email}'") from exc
        if (
            address.version != 4
            or address.network.prefixlen != 32
            or address.ip not in ipaddress.ip_network("172.19.0.0/16")
        ):
            raise ValueError(f"Invalid WireGuard address for '{client.email}'")
        offset = int(address.ip) - int(ipaddress.ip_address("172.19.0.0"))
        if offset < 2 or offset > 65533:
            raise ValueError(f"Invalid WireGuard address for '{client.email}'")
        if offset in used:
            raise ValueError(f"Duplicate WireGuard address: {address}")
        used.add(offset)
        result[client.id] = str(address)
    return result, used


def wireguard_addresses(inbound):
    if inbound.protocol != "wireguard":
        return {}
    clients = Client.query.filter_by(inbound_tag=inbound.tag).all()
    return _assigned_addresses(clients)[0]


def ensure_wireguard_addresses(inbound):
    if inbound.protocol != "wireguard":
        return {}
    clients = Client.query.filter_by(inbound_tag=inbound.tag).all()
    result = assign_wireguard_addresses(clients)
    db.session.flush()
    return result


def assign_wireguard_addresses(clients):
    result, used = _assigned_addresses(clients, allow_missing=True)
    pending = [client for client in clients if not client.wg_address]
    pending.sort(key=lambda client: not client.enable)
    for client in pending:
        base = int.from_bytes(hashlib.sha256(str(client.id).encode()).digest()[:4], "big") % 65532
        for probe in range(65532):
            offset = 2 + ((base + probe) % 65532)
            if offset not in used:
                used.add(offset)
                high, low = divmod(offset, 256)
                client.wg_address = f"172.19.{high}.{low}/32"
                result[client.id] = client.wg_address
                break
        else:
            raise ValueError("WireGuard peer address space exhausted")
    return result
