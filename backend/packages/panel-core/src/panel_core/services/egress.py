import ipaddress
import os

DEFAULT_POOL_RANGE = "172.28.0.128-172.28.0.254"
DEFAULT_BIND_PREFIX = 24


def _valid_ip(v):
    if not v:
        return True
    try:
        ipaddress.IPv4Address(v)
        return True
    except ValueError:
        return False


def get_pool_range():
    raw = os.environ.get("EGRESS_BIND_POOL_RANGE", DEFAULT_POOL_RANGE)
    start_s, _, end_s = raw.partition("-")
    return (
        ipaddress.IPv4Address(start_s.strip()),
        ipaddress.IPv4Address(end_s.strip()),
    )


def get_bind_prefix():
    try:
        return int(os.environ.get("EGRESS_BIND_PREFIX", DEFAULT_BIND_PREFIX))
    except (TypeError, ValueError):
        return DEFAULT_BIND_PREFIX


def allocate_bind_ip(used_ips):
    used = {str(ip).strip() for ip in used_ips if ip and str(ip).strip()}
    start, end = get_pool_range()
    current = start
    while current <= end:
        candidate = str(current)
        if candidate not in used:
            return candidate
        current += 1
    raise ValueError("No free egress bind-IP left in pool; expand the subnet")


def build_bind_ips():
    from panel_core.models import Outbound

    prefix = get_bind_prefix()
    rows = (
        Outbound.query.filter(Outbound.send_through.isnot(None), Outbound.send_through != "")
        .order_by(Outbound.send_through)
        .all()
    )
    return [{"send_through": o.send_through, "prefix": prefix} for o in rows if _valid_ip(o.send_through)]


def build_host_plan():
    from panel_core.models import Outbound

    rows = (
        Outbound.query.filter(Outbound.public_ip.isnot(None), Outbound.public_ip != "")
        .order_by(Outbound.public_ip)
        .all()
    )
    return [
        {
            "tag": o.tag,
            "public_ip": o.public_ip,
            "send_through": o.send_through or "",
            "gateway": o.gateway or "",
        }
        for o in rows
        if _valid_ip(o.public_ip) and _valid_ip(o.send_through) and _valid_ip(o.gateway)
    ]
