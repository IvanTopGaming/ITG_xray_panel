"""Populate panel.db with realistic demo data covering all UI surfaces.

Run inside the backend container:

    docker-compose exec backend python /app/scripts/seed_demo.py

Idempotent: tagged demo rows are wiped and re-created on every run.
"""

from __future__ import annotations

import json
import math
import random
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, "/app")

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (  # noqa: E402
    Balancer,
    Client,
    DomainStat,
    Inbound,
    Node,
    NodeClientTraffic,
    Outbound,
    RoutingProfile,
    TrafficSnapshot,
)

DEMO_PREFIX = "demo-"
DEMO_NODE_PREFIX = "demo-node-"

random.seed(20260516)


# ─── Inbound catalog ─────────────────────────────────────────────────────────
INBOUNDS = [
    # tag, port, protocol, stream_settings dict, routing_profile_name (or None)
    (
        f"{DEMO_PREFIX}vless-reality",
        443,
        "vless",
        {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "dest": "www.google.com:443",
                "serverNames": ["www.google.com"],
                "privateKey": "qG_9OdNYnWS7JkeAp4i986Ckz7RVcfsrcaGdKDR2Vmg",
                "publicKey": "l3yU0kCuVhNtbjLnUzvv6nbwi5BUIbiOjAonWIIqLkg",
                "shortIds": [""],
                "fingerprint": "chrome",
            },
        },
        f"{DEMO_PREFIX}ru-direct",  # this inbound routes Russian traffic direct
    ),
    (
        f"{DEMO_PREFIX}vmess-ws",
        10086,
        "vmess",
        {
            "network": "ws",
            "security": "none",
            "wsSettings": {"path": "/vmess", "headers": {"Host": "cdn.example.com"}},
        },
        f"{DEMO_PREFIX}streaming-balanced",
    ),
    (
        f"{DEMO_PREFIX}trojan-tls",
        8443,
        "trojan",
        {
            "network": "tcp",
            "security": "tls",
            "tlsSettings": {"serverName": "trojan.example.com"},
        },
        f"{DEMO_PREFIX}ads-block",
    ),
    (
        f"{DEMO_PREFIX}ss-2022",
        2086,
        "shadowsocks",
        {
            "network": "tcp",
            "security": "none",
            "ssMethod": "2022-blake3-aes-128-gcm",
            "ssPassword": "demo16BytesPad==",
            "ssNetwork": "tcp",
        },
        None,
    ),
    (
        f"{DEMO_PREFIX}socks-bot",
        1080,
        "socks",
        {"network": "tcp", "security": "none", "authUser": "bot", "authPass": "demo"},
        None,
    ),
    (
        f"{DEMO_PREFIX}http-bot",
        8118,
        "http",
        {"network": "tcp", "security": "none", "authUser": "bot", "authPass": "demo"},
        None,
    ),
]


# ─── Outbound catalog ────────────────────────────────────────────────────────
OUTBOUNDS = [
    # tag, protocol, settings, stream_settings
    (
        f"{DEMO_PREFIX}proxy-eu",
        "vless",
        {
            "vnext": [
                {
                    "address": "eu.upstream.example.com",
                    "port": 443,
                    "users": [{"id": "11111111-2222-3333-4444-555555555555", "encryption": "none"}],
                }
            ]
        },
        {"network": "tcp", "security": "tls"},
    ),
    (
        f"{DEMO_PREFIX}proxy-us",
        "vless",
        {
            "vnext": [
                {
                    "address": "us.upstream.example.com",
                    "port": 443,
                    "users": [{"id": "66666666-7777-8888-9999-aaaaaaaaaaaa", "encryption": "none"}],
                }
            ]
        },
        {"network": "tcp", "security": "tls"},
    ),
    (
        f"{DEMO_PREFIX}socks-upstream",
        "socks",
        {
            "servers": [
                {
                    "address": "socks.upstream.example.com",
                    "port": 1080,
                    "users": [{"user": "demo", "pass": "demo"}],
                }
            ]
        },
        {"network": "tcp", "security": "none"},
    ),
]

# ─── Balancer ────────────────────────────────────────────────────────────────
BALANCERS = [
    # tag, selector (list of outbound tags), strategy, fallback_tag
    (
        f"{DEMO_PREFIX}eu-us-balancer",
        [f"{DEMO_PREFIX}proxy-eu", f"{DEMO_PREFIX}proxy-us"],
        "random",
        f"{DEMO_PREFIX}proxy-eu",
    ),
]

# ─── Routing profiles ────────────────────────────────────────────────────────
ROUTING_PROFILES = [
    # name, rules (list of dicts)
    (
        f"{DEMO_PREFIX}ru-direct",
        [
            {
                "type": "field",
                "enabled": True,
                "comment": "Route .ru domains direct",
                "domain": ["geosite:category-gov-ru", "regexp:.*\\.ru$"],
                "outboundTag": "direct",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "Default → eu-us balancer",
                "network": "tcp,udp",
                "outboundTag": f"{DEMO_PREFIX}eu-us-balancer",
            },
        ],
    ),
    (
        f"{DEMO_PREFIX}ads-block",
        [
            {
                "type": "field",
                "enabled": True,
                "comment": "Block ad/tracker domains",
                "domain": [
                    "geosite:category-ads-all",
                    "doubleclick.net",
                    "googleadservices.com",
                ],
                "outboundTag": "block",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "Block ad IP ranges",
                "ip": ["geoip:private", "127.0.0.0/8"],
                "outboundTag": "block",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "Default → EU proxy",
                "network": "tcp,udp",
                "outboundTag": f"{DEMO_PREFIX}proxy-eu",
            },
        ],
    ),
    (
        f"{DEMO_PREFIX}streaming-balanced",
        [
            {
                "type": "field",
                "enabled": True,
                "comment": "Streaming → US",
                "domain": ["netflix.com", "googlevideo.com", "youtube.com"],
                "outboundTag": f"{DEMO_PREFIX}proxy-us",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "Russia direct",
                "domain": ["regexp:.*\\.ru$", "yandex.ru", "vk.com"],
                "outboundTag": "direct",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "Default → balancer",
                "network": "tcp,udp",
                "outboundTag": f"{DEMO_PREFIX}eu-us-balancer",
            },
        ],
    ),
]


# ─── User catalog with rich variation ────────────────────────────────────────
# Each user gets its own random profile (peak, schedule, online_chance, bursts).
# email, inbound_tag, archetype
USER_ARCHETYPES = [
    # archetype controls the *shape* of activity, not the volume.
    # 'night-owl': active 22:00–06:00
    # 'day-worker': active 09:00–18:00, weekdays only
    # 'evening': peak 18:00–24:00 every day
    # 'all-day': uniform 08:00–24:00
    # 'weekend-warrior': active mostly Sat/Sun, big bursts
    # 'sporadic': low online_chance, occasional bursts
    # 'idle': barely connects
    "evening",
    "night-owl",
    "day-worker",
    "all-day",
    "weekend-warrior",
    "sporadic",
    "idle",
]

USER_EMAILS = [
    ("alice", f"{DEMO_PREFIX}vless-reality"),
    ("bob", f"{DEMO_PREFIX}vless-reality"),
    ("carol", f"{DEMO_PREFIX}vless-reality"),
    ("dave", f"{DEMO_PREFIX}vless-reality"),
    ("eve", f"{DEMO_PREFIX}vless-reality"),
    ("frank", f"{DEMO_PREFIX}vless-reality"),
    ("grace", f"{DEMO_PREFIX}vless-reality"),
    ("hank", f"{DEMO_PREFIX}vless-reality"),
    ("iris", f"{DEMO_PREFIX}vless-reality"),
    ("jack", f"{DEMO_PREFIX}vless-reality"),
    ("kate", f"{DEMO_PREFIX}vless-reality"),
    ("leo", f"{DEMO_PREFIX}vless-reality"),
    ("heidi", f"{DEMO_PREFIX}vmess-ws"),
    ("ivan", f"{DEMO_PREFIX}vmess-ws"),
    ("judy", f"{DEMO_PREFIX}vmess-ws"),
    ("kevin", f"{DEMO_PREFIX}vmess-ws"),
    ("luna", f"{DEMO_PREFIX}vmess-ws"),
    ("mallory", f"{DEMO_PREFIX}trojan-tls"),
    ("niaj", f"{DEMO_PREFIX}trojan-tls"),
    ("olivia", f"{DEMO_PREFIX}trojan-tls"),
    ("peggy", f"{DEMO_PREFIX}trojan-tls"),
    ("ron", f"{DEMO_PREFIX}trojan-tls"),
    ("quinn", f"{DEMO_PREFIX}ss-2022"),
    ("ruth", f"{DEMO_PREFIX}ss-2022"),
    ("steve", f"{DEMO_PREFIX}ss-2022"),
    ("trent", f"{DEMO_PREFIX}ss-2022"),
    ("uma", f"{DEMO_PREFIX}ss-2022"),
    ("victor", f"{DEMO_PREFIX}ss-2022"),
    ("wendy", f"{DEMO_PREFIX}ss-2022"),
    ("xavier", f"{DEMO_PREFIX}vless-reality"),
    ("yara", f"{DEMO_PREFIX}vmess-ws"),
    ("zoe", f"{DEMO_PREFIX}trojan-tls"),
]


def build_user_profiles():
    """Assign each user a unique random activity profile."""
    archetype_distribution = (
        ["evening"] * 8
        + ["night-owl"] * 4
        + ["day-worker"] * 5
        + ["all-day"] * 4
        + ["weekend-warrior"] * 3
        + ["sporadic"] * 4
        + ["idle"] * 4
    )
    random.shuffle(archetype_distribution)
    profiles = {}
    for (name, tag), archetype in zip(USER_EMAILS, archetype_distribution):
        # Pareto-ish per-user peak: most users medium, few power users.
        # peak bytes/hour
        roll = random.random()
        if archetype == "idle":
            peak = random.randint(300_000, 2_500_000)  # 0.3–2.5 MB/h
        elif archetype == "sporadic":
            peak = random.randint(8_000_000, 80_000_000)  # 8–80 MB
        elif roll < 0.10:  # 10% power users
            peak = random.randint(800_000_000, 2_500_000_000)  # 800 MB – 2.5 GB
        elif roll < 0.35:  # heavy users
            peak = random.randint(200_000_000, 800_000_000)
        elif roll < 0.75:  # medium
            peak = random.randint(30_000_000, 200_000_000)
        else:  # light
            peak = random.randint(3_000_000, 30_000_000)

        profiles[(f"{name}@vpn", tag)] = {
            "archetype": archetype,
            "peak": peak,
            "online_chance": {
                "evening": random.uniform(0.80, 0.95),
                "night-owl": random.uniform(0.55, 0.85),
                "day-worker": random.uniform(0.75, 0.92),
                "all-day": random.uniform(0.88, 0.98),
                "weekend-warrior": random.uniform(0.45, 0.65),
                "sporadic": random.uniform(0.10, 0.35),
                "idle": random.uniform(0.02, 0.10),
            }[archetype],
            "burst_chance": random.uniform(0.005, 0.04),  # per-hour chance of 5–15x spike
            "asymmetry": random.uniform(0.10, 0.35),  # upload share of total
            "preferred_pool": random.choice(["ru", "ru", "ru", "eu", "asia"]),  # 60% RU
        }
    return profiles


def hour_weight(archetype: str, hour: int, weekday: int) -> float:
    """0..1 weight modulating peak by time-of-day and day-of-week."""
    if archetype == "evening":
        base = max(0.05, 1.0 - abs(hour - 20) / 8)
    elif archetype == "night-owl":
        # peak at 2am, decays by ~10
        dist = min(abs(hour - 2), abs(hour - 26)) / 6
        base = max(0.05, 1.0 - dist)
    elif archetype == "day-worker":
        base = max(0.02, 1.0 - abs(hour - 13) / 7)
        if weekday >= 5:
            base *= 0.20
    elif archetype == "all-day":
        # broad plateau 08-23, low 00-07
        base = 0.30 if hour < 8 else 0.85
        if 18 <= hour <= 22:
            base = 1.0
    elif archetype == "weekend-warrior":
        if weekday >= 5:
            base = max(0.20, 1.0 - abs(hour - 16) / 10)
        else:
            base = 0.10
    elif archetype == "sporadic":
        base = 0.3 + 0.4 * math.sin((hour / 24) * 2 * math.pi + weekday)
        base = max(0.05, base)
    else:  # idle
        base = 0.10
    return base


def hourly_bytes(profile: dict, hour: int, weekday: int) -> tuple[int, int] | None:
    """Returns (up, down) for this hour, or None if user was offline."""
    if random.random() > profile["online_chance"]:
        return None
    weight = hour_weight(profile["archetype"], hour, weekday)
    if weight <= 0:
        return None
    base = profile["peak"] * weight * random.uniform(0.55, 1.45)
    # bursts: 0.5% – 4% chance of 5x – 15x spike
    if random.random() < profile["burst_chance"]:
        base *= random.uniform(5.0, 15.0)
    if base < 1024:
        return None
    total = int(base)
    up = int(total * profile["asymmetry"])
    down = total - up
    return up, down


# ─── IP & domain pools ───────────────────────────────────────────────────────
DEMO_IP_POOLS = {
    "ru": ["87.117.190.{}", "95.31.4.{}", "178.140.6.{}", "5.182.99.{}", "213.87.12.{}", "188.123.231.{}"],
    "eu": ["88.99.1.{}", "94.130.45.{}", "159.69.7.{}", "144.76.12.{}", "85.10.200.{}"],
    "us": ["198.51.100.{}", "203.0.113.{}", "192.0.2.{}"],
    "asia": ["210.142.92.{}", "139.99.45.{}", "103.244.50.{}"],
}

DEMO_DOMAINS = [
    "google.com", "youtube.com", "googlevideo.com", "instagram.com", "facebook.com",
    "telegram.org", "github.com", "twitter.com", "x.com", "tiktok.com",
    "cloudflare.com", "amazon.com", "netflix.com", "openai.com", "anthropic.com",
    "discord.com", "spotify.com", "reddit.com", "wikipedia.org", "stackoverflow.com",
    "linkedin.com", "pinterest.com", "duckduckgo.com", "vk.com", "yandex.ru", "ya.ru",
    "habr.com", "lenta.ru", "rbc.ru", "kinopoisk.ru",
]


def gen_ip(pool: str) -> str:
    return random.choice(DEMO_IP_POOLS[pool]).format(random.randint(1, 254))


def pick_ips(profile: dict) -> list[str]:
    if profile["archetype"] == "idle":
        return [] if random.random() < 0.35 else [gen_ip(profile["preferred_pool"])]
    if profile["archetype"] == "sporadic":
        count = random.randint(1, 3)
    elif profile["peak"] > 500_000_000:  # power users have many devices
        count = random.randint(5, 9)
    elif profile["peak"] > 100_000_000:
        count = random.randint(3, 6)
    else:
        count = random.randint(1, 4)
    seen, out = set(), []
    primary = profile["preferred_pool"]
    for _ in range(count):
        pool = primary if random.random() < 0.78 else random.choice(list(DEMO_IP_POOLS))
        ip = gen_ip(pool)
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out[:10]


# ─── Limit/expiry/state diversity ────────────────────────────────────────────


def assign_constraints(name: str, profile: dict) -> dict:
    """Generate a mix of limit/expiry/state for each user."""
    now_ms = int(datetime.now().timestamp() * 1000)
    day_ms = 86_400_000

    # ~15% disabled users
    enable = random.random() > 0.15

    # limit_bytes mix:
    # 30% unlimited, 25% 5 GB, 20% 50 GB, 15% 200 GB, 10% 1 TB
    r = random.random()
    if r < 0.30:
        limit_bytes = 0
    elif r < 0.55:
        limit_bytes = 5 * 1024**3
    elif r < 0.75:
        limit_bytes = 50 * 1024**3
    elif r < 0.90:
        limit_bytes = 200 * 1024**3
    else:
        limit_bytes = 1024**4

    # expiry mix:
    # 25% no expiry, 25% in 30+ days, 20% in 3 days, 15% in <24h, 15% already expired
    r = random.random()
    if r < 0.25:
        expiry_time = 0
    elif r < 0.50:
        expiry_time = now_ms + random.randint(30, 90) * day_ms
    elif r < 0.70:
        expiry_time = now_ms + random.randint(2, 4) * day_ms
    elif r < 0.85:
        expiry_time = now_ms + random.randint(1, 23) * 3_600_000
    else:
        expiry_time = now_ms - random.randint(1, 7) * day_ms  # already expired

    # device_limit: 0 = unlimited; 30% have device limits 1/2/5
    if random.random() < 0.30:
        device_limit = random.choice([1, 2, 5])
    else:
        device_limit = 0

    # global_limit_bytes (aggregate across master + nodes): 20% have it
    global_limit = 0
    if random.random() < 0.20:
        global_limit = limit_bytes * 3 if limit_bytes else 500 * 1024**3

    # allowed_node_groups: 25% restricted to one group
    if random.random() < 0.25:
        node_groups = random.choice(["eu", "us", "asia", "eu,us"])
    else:
        node_groups = ""

    # reset_day: 30% have monthly reset
    reset_day = random.choice([0, 1, 5, 15]) if random.random() < 0.3 else 0

    # last_seen scaled to archetype
    arch = profile["archetype"]
    if arch == "idle":
        last_seen = now_ms - random.randint(2, 60) * day_ms
    elif arch in ("sporadic",):
        last_seen = now_ms - random.randint(1, 14) * day_ms
    elif arch == "day-worker" and datetime.now().weekday() >= 5:
        last_seen = now_ms - random.randint(1, 3) * day_ms
    elif profile["peak"] > 500_000_000:
        last_seen = now_ms - random.randint(0, 20) * 60_000  # within 20min
    else:
        last_seen = now_ms - random.randint(5, 24 * 60) * 60_000

    # flow only for vless
    flow = "xtls-rprx-vision" if random.random() < 0.5 else None

    return {
        "limit_bytes": limit_bytes,
        "expiry_time": expiry_time,
        "device_limit": device_limit if device_limit else None,
        "global_limit_bytes": global_limit,
        "allowed_node_groups": node_groups,
        "reset_day": reset_day,
        "last_seen": last_seen,
        "enable": enable,
        "flow": flow,
    }


# ─── Main seeder ──────────────────────────────────────────────────────────────


def wipe_existing_demo():
    print("→ Wiping existing demo rows...")
    demo_inbound_tags = [t for (t, _, _, _, _) in INBOUNDS]
    demo_outbound_tags = [t for (t, _, _, _) in OUTBOUNDS]
    demo_balancer_tags = [t for (t, _, _, _) in BALANCERS]
    demo_profile_names = [n for (n, _) in ROUTING_PROFILES]
    demo_emails = [f"{n}@vpn" for (n, _) in USER_EMAILS]

    DomainStat.query.filter(DomainStat.inbound_tag.in_(demo_inbound_tags)).delete(synchronize_session=False)
    TrafficSnapshot.query.filter(
        (TrafficSnapshot.inbound_tag.in_(demo_inbound_tags))
        | (
            (TrafficSnapshot.entity_type == "inbound")
            & (TrafficSnapshot.entity_id.in_(demo_inbound_tags))
        )
    ).delete(synchronize_session=False)
    NodeClientTraffic.query.filter(NodeClientTraffic.email.in_(demo_emails)).delete(synchronize_session=False)
    Client.query.filter(Client.inbound_tag.in_(demo_inbound_tags)).delete(synchronize_session=False)
    Inbound.query.filter(Inbound.tag.in_(demo_inbound_tags)).delete(synchronize_session=False)
    Balancer.query.filter(Balancer.tag.in_(demo_balancer_tags)).delete(synchronize_session=False)
    Outbound.query.filter(Outbound.tag.in_(demo_outbound_tags)).delete(synchronize_session=False)
    RoutingProfile.query.filter(RoutingProfile.name.in_(demo_profile_names)).delete(synchronize_session=False)
    Node.query.filter(Node.name.like(f"{DEMO_NODE_PREFIX}%")).delete(synchronize_session=False)
    db.session.commit()


def create_outbounds_balancers_routing():
    print("→ Creating outbounds...")
    for tag, protocol, settings, stream in OUTBOUNDS:
        db.session.add(
            Outbound(
                tag=tag,
                protocol=protocol,
                enable=True,
                settings=json.dumps(settings),
                stream_settings=json.dumps(stream),
                mux="{}",
            )
        )
    db.session.commit()

    print("→ Creating balancer(s)...")
    for tag, selector, strategy, fallback in BALANCERS:
        db.session.add(
            Balancer(
                tag=tag,
                enable=True,
                selector=json.dumps(selector),
                strategy=strategy,
                fallback_tag=fallback,
            )
        )
    db.session.commit()

    print("→ Creating routing profiles...")
    for name, rules in ROUTING_PROFILES:
        db.session.add(
            RoutingProfile(
                name=name,
                rules=json.dumps(rules),
                enable=True,
            )
        )
    db.session.commit()


def create_inbounds():
    print("→ Creating inbounds...")
    profile_map = {p.name: p.id for p in RoutingProfile.query.all()}
    for tag, port, protocol, stream, profile_name in INBOUNDS:
        ib = Inbound(
            tag=tag,
            port=port,
            protocol=protocol,
            stream_settings=json.dumps(stream),
            routing_profile_id=profile_map.get(profile_name) if profile_name else None,
            up=0,
            down=0,
        )
        db.session.add(ib)
    db.session.commit()


def create_clients(user_profiles) -> dict[tuple[str, str], Client]:
    print("→ Creating clients with diverse constraints...")
    by_key: dict[tuple[str, str], Client] = {}
    for (name, tag), profile in user_profiles.items():
        email = name  # already 'name@vpn'
        constraints = assign_constraints(email, profile)
        ips = pick_ips(profile)
        c = Client(
            id=str(uuid.uuid4()),
            email=email,
            inbound_tag=tag,
            limit_bytes=constraints["limit_bytes"],
            expiry_time=constraints["expiry_time"],
            up=0,
            down=0,
            enable=constraints["enable"],
            last_seen=constraints["last_seen"],
            source_ips=json.dumps(ips),
            global_limit_bytes=constraints["global_limit_bytes"],
            allowed_node_groups=constraints["allowed_node_groups"],
            reset_day=constraints["reset_day"],
            device_limit=constraints["device_limit"],
            flow=constraints["flow"],
        )
        db.session.add(c)
        by_key[(email, tag)] = c
    db.session.commit()
    return by_key


def create_traffic_snapshots(user_profiles, clients_by_key):
    print("→ Generating 90 days of hourly traffic snapshots (per-user unique patterns)...")
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=90)

    user_totals = {k: [0, 0] for k in clients_by_key}
    inbound_totals = {tag: [0, 0] for (tag, _, _, _, _) in INBOUNDS}

    rows_user = []
    rows_inbound = []
    cur = start
    while cur <= now:
        bucket_ts = int(cur.timestamp())
        weekday = cur.weekday()
        hour = cur.hour
        per_inbound_up = {tag: 0 for (tag, _, _, _, _) in INBOUNDS}
        per_inbound_down = {tag: 0 for (tag, _, _, _, _) in INBOUNDS}

        for (email, tag), profile in user_profiles.items():
            result = hourly_bytes(profile, hour, weekday)
            if result is None:
                continue
            up, down = result
            rows_user.append(
                {
                    "entity_type": "user",
                    "entity_id": email,
                    "inbound_tag": tag,
                    "bucket": bucket_ts,
                    "up": up,
                    "down": down,
                }
            )
            user_totals[(email, tag)][0] += up
            user_totals[(email, tag)][1] += down
            per_inbound_up[tag] += up
            per_inbound_down[tag] += down

        for tag in per_inbound_up:
            if per_inbound_up[tag] or per_inbound_down[tag]:
                rows_inbound.append(
                    {
                        "entity_type": "inbound",
                        "entity_id": tag,
                        "inbound_tag": "",
                        "bucket": bucket_ts,
                        "up": per_inbound_up[tag],
                        "down": per_inbound_down[tag],
                    }
                )
                inbound_totals[tag][0] += per_inbound_up[tag]
                inbound_totals[tag][1] += per_inbound_down[tag]
        cur += timedelta(hours=1)

    print(f"   {len(rows_user)} user-rows, {len(rows_inbound)} inbound-rows — bulk insert...")
    if rows_user:
        db.session.bulk_insert_mappings(TrafficSnapshot, rows_user)
    if rows_inbound:
        db.session.bulk_insert_mappings(TrafficSnapshot, rows_inbound)
    db.session.commit()

    for (email, tag), (up, down) in user_totals.items():
        c = clients_by_key[(email, tag)]
        c.up = up
        c.down = down
    for tag, (up, down) in inbound_totals.items():
        ib = Inbound.query.filter_by(tag=tag).first()
        if ib:
            ib.up = up
            ib.down = down
    db.session.commit()


def create_domain_stats(user_profiles):
    print("→ Generating 30 days of domain stats...")
    today = datetime.now().date()
    rows = []
    for days_ago in range(30):
        date_str = (today - timedelta(days=days_ago)).isoformat()
        for (email, tag), profile in user_profiles.items():
            if profile["archetype"] == "idle" and random.random() < 0.85:
                continue
            if random.random() > profile["online_chance"] * 0.9:
                continue  # skipped that day
            # hits scale with profile peak (rough)
            scale = max(5, int(profile["peak"] / 5_000_000))
            n_domains = random.randint(4, 14)
            picks = random.sample(DEMO_DOMAINS, n_domains)
            for domain in picks:
                hits = max(1, int(scale * random.uniform(0.03, 0.4) / n_domains))
                rows.append(
                    {
                        "date": date_str,
                        "domain": domain,
                        "client_email": email,
                        "inbound_tag": tag,
                        "hit_count": hits,
                    }
                )
    print(f"   {len(rows)} domain-stat rows — bulk insert...")
    if rows:
        db.session.bulk_insert_mappings(DomainStat, rows)
    db.session.commit()


def create_nodes(user_profiles):
    print("→ Creating demo nodes...")
    now_ms = int(datetime.now().timestamp() * 1000)
    nodes = [
        # name, status, last_error, enable, groups
        (f"{DEMO_NODE_PREFIX}eu-fra", "online", "", True, "eu"),
        (f"{DEMO_NODE_PREFIX}us-nyc", "offline", "connection refused", True, "us"),
        (f"{DEMO_NODE_PREFIX}asia-sgp", "online", "", True, "asia"),
        (f"{DEMO_NODE_PREFIX}eu-ams", "online", "", False, "eu"),  # disabled, still listed
    ]
    created = []
    for name, status, err, enable, groups in nodes:
        n = Node(
            name=name,
            url=f"https://{name}.example.com",
            username="admin",
            password="demo",
            inbound_tag=f"{DEMO_PREFIX}vless-reality",
            enable=enable,
            sync_users=True,
            sync_inbound=False,
            status=status,
            last_check=now_ms,
            last_error=err,
            groups=groups,
            strict_mirror=False,
        )
        db.session.add(n)
        created.append(n)
    db.session.commit()

    print("→ Creating node client traffic samples (varied per node)...")
    online = [n for n in created if n.status == "online" and n.enable]
    rows = []
    for n in online:
        for (email, tag), profile in user_profiles.items():
            # Only users whose allowed_node_groups matches this node's group
            c = Client.query.filter_by(email=email, inbound_tag=tag).first()
            if c and c.allowed_node_groups:
                groups = [g.strip() for g in c.allowed_node_groups.split(",") if g.strip()]
                if n.groups not in groups:
                    continue
            # 30% of remaining users skip this node
            if random.random() < 0.30:
                continue
            mult = profile["peak"] / 1_000_000_000  # GB/h baseline
            # Each node sees a fraction of user's traffic
            node_share = random.uniform(0.05, 0.5)
            up = int(mult * node_share * 100_000_000 * random.uniform(0.5, 1.8))
            down = int(up * random.uniform(2.5, 7.0))
            rows.append(
                {
                    "node_id": n.id,
                    "email": email,
                    "up": max(0, up),
                    "down": max(0, down),
                    "last_polled": now_ms,
                }
            )
    if rows:
        db.session.bulk_insert_mappings(NodeClientTraffic, rows)
    db.session.commit()
    print(f"   {len(rows)} per-node traffic rows")


def main():
    app = create_app()
    with app.app_context():
        wipe_existing_demo()
        create_outbounds_balancers_routing()
        create_inbounds()
        user_profiles = build_user_profiles()
        clients_by_key = create_clients(user_profiles)
        create_traffic_snapshots(user_profiles, clients_by_key)
        create_domain_stats(user_profiles)
        create_nodes(user_profiles)

        print("\n─── Seed complete ───")
        print(f"  Routing profiles:   {RoutingProfile.query.count()}")
        print(f"  Outbounds:          {Outbound.query.count()}")
        print(f"  Balancers:          {Balancer.query.count()}")
        print(f"  Inbounds:           {Inbound.query.count()}")
        print(f"  Clients:            {Client.query.count()}")
        print(f"  Traffic snapshots:  {TrafficSnapshot.query.count()}")
        print(f"  Domain stats:       {DomainStat.query.count()}")
        print(f"  Nodes:              {Node.query.count()}")
        print(f"  Node-client rows:   {NodeClientTraffic.query.count()}")

        # Constraint diversity breakdown
        clients = Client.query.filter(Client.inbound_tag.like(f"{DEMO_PREFIX}%")).all()
        disabled = sum(1 for c in clients if not c.enable)
        expired = sum(1 for c in clients if c.expiry_time and c.expiry_time < int(datetime.now().timestamp() * 1000))
        with_limit = sum(1 for c in clients if c.limit_bytes > 0)
        with_global = sum(1 for c in clients if c.global_limit_bytes > 0)
        with_group_filter = sum(1 for c in clients if c.allowed_node_groups)
        with_device_lim = sum(1 for c in clients if c.device_limit)
        no_ips = sum(1 for c in clients if c.source_ips == "[]" or not c.source_ips)
        print("\n  Client constraint diversity:")
        print(f"    disabled:               {disabled}/{len(clients)}")
        print(f"    expired:                {expired}/{len(clients)}")
        print(f"    byte limit set:         {with_limit}/{len(clients)}")
        print(f"    global limit set:       {with_global}/{len(clients)}")
        print(f"    node group restricted:  {with_group_filter}/{len(clients)}")
        print(f"    device limit set:       {with_device_lim}/{len(clients)}")
        print(f"    empty source_ips:       {no_ips}/{len(clients)}")


if __name__ == "__main__":
    main()
