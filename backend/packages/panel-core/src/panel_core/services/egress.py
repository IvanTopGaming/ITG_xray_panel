import ipaddress
import os
import re

DEFAULT_POOL_RANGE = "172.28.0.128-172.28.0.254"
DEFAULT_BIND_PREFIX = 24
DEFAULT_UPLINK_IFACE = "eth0"


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


def build_host_script(iface=None):
    from panel_core.models import Outbound

    iface = iface or os.environ.get("EGRESS_UPLINK_IFACE", DEFAULT_UPLINK_IFACE)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", iface or ""):
        raise ValueError("Invalid egress uplink interface name")
    rows = (
        Outbound.query.filter(Outbound.public_ip.isnot(None), Outbound.public_ip != "")
        .order_by(Outbound.public_ip)
        .all()
    )

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "iptables -t nat -N EGRESS_SNAT 2>/dev/null || true",
        "iptables -t nat -C POSTROUTING -j EGRESS_SNAT 2>/dev/null || iptables -t nat -I POSTROUTING -j EGRESS_SNAT",
        "iptables -t nat -F EGRESS_SNAT",
        "",
    ]

    table = 100
    for o in rows:
        if not (_valid_ip(o.public_ip) and _valid_ip(o.send_through) and _valid_ip(o.gateway)):
            continue
        pub = o.public_ip
        lines.append(f"ip addr show dev {iface} | grep -qw {pub} || ip addr add {pub}/32 dev {iface}")
        if o.send_through:
            lines.append(f"iptables -t nat -A EGRESS_SNAT -s {o.send_through} -j SNAT --to-source {pub}")
        if o.gateway:
            lines.append(f'ip rule list | grep -q "from {pub} lookup {table}" || ip rule add from {pub} table {table}')
            lines.append(
                f"ip route show table {table} | grep -q default || "
                f"ip route add default via {o.gateway} dev {iface} table {table}"
            )
            table += 1
        lines.append("")

    return "\n".join(lines) + "\n"
