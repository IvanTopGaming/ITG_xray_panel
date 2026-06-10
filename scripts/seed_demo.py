

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
    Outbound,
    RoutingProfile,
    TrafficSnapshot,
)

DEMO_PREFIX = "demo-"

random.seed(20260522)





INBOUNDS = [
    {
        "tag": f"{DEMO_PREFIX}vless-reality-vision",
        "port": 14443,
        "protocol": "vless",
        "label": "🇩🇪 Frankfurt — VLESS Reality (Vision)",
        "routing": f"{DEMO_PREFIX}ru-direct",
        "device_limit": 5,
        "fallback": None,
        "stream": {
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
    },
    {
        "tag": f"{DEMO_PREFIX}vless-grpc",
        "port": 8443,
        "protocol": "vless",
        "label": "🇳🇱 Amsterdam — VLESS gRPC TLS",
        "routing": f"{DEMO_PREFIX}streaming-balanced",
        "device_limit": 3,
        "fallback": None,
        "stream": {
            "network": "grpc",
            "security": "tls",
            "tlsSettings": {
                "serverName": "ams.example.com",
                "alpn": ["h2"],
                "_utlsFingerprint": "chrome",
            },
            "grpcSettings": {
                "serviceName": "vless-grpc",
                "multiMode": True,
            },
        },
    },
    {
        "tag": f"{DEMO_PREFIX}vless-xhttp",
        "port": 8444,
        "protocol": "vless",
        "label": "🇬🇧 London — VLESS XHTTP TLS",
        "routing": f"{DEMO_PREFIX}streaming-balanced",
        "device_limit": 0,
        "fallback": None,
        "stream": {
            "network": "xhttp",
            "security": "tls",
            "tlsSettings": {
                "serverName": "lon.example.com",
                "alpn": ["h2", "http/1.1"],
            },
            "xhttpSettings": {
                "path": "/xhttp",
                "host": "lon.example.com",
                "mode": "auto",
            },
        },
    },
    {
        "tag": f"{DEMO_PREFIX}vmess-ws",
        "port": 8081,
        "protocol": "vmess",
        "label": "🇺🇸 New York — VMess WebSocket TLS (CDN)",
        "routing": f"{DEMO_PREFIX}streaming-balanced",
        "device_limit": 0,
        "fallback": None,
        "stream": {
            "network": "ws",
            "security": "tls",
            "tlsSettings": {
                "serverName": "cdn.example.com",
                "alpn": ["http/1.1"],
            },
            "wsSettings": {
                "path": "/vmess",
                "headers": {"Host": "cdn.example.com"},
            },
        },
    },
    {
        "tag": f"{DEMO_PREFIX}vmess-httpupgrade",
        "port": 8082,
        "protocol": "vmess",
        "label": "🇫🇮 Helsinki — VMess HTTPUpgrade",
        "routing": f"{DEMO_PREFIX}work-vpn",
        "device_limit": 2,
        "fallback": None,
        "stream": {
            "network": "httpupgrade",
            "security": "tls",
            "tlsSettings": {
                "serverName": "hel.example.com",
                "alpn": ["http/1.1"],
            },
            "httpUpgradeSettings": {
                "path": "/vmess-hu",
                "host": "hel.example.com",
            },
        },
    },
    {
        "tag": f"{DEMO_PREFIX}trojan-tls",
        "port": 8447,
        "protocol": "trojan",
        "label": "🇫🇷 Paris — Trojan TLS",
        "routing": f"{DEMO_PREFIX}ads-block",
        "device_limit": 0,
        "fallback": None,
        "stream": {
            "network": "tcp",
            "security": "tls",
            "tlsSettings": {
                "serverName": "par.example.com",
                "alpn": ["http/1.1"],
            },
        },
    },
    {
        "tag": f"{DEMO_PREFIX}trojan-splithttp",
        "port": 8448,
        "protocol": "trojan",
        "label": "🇨🇭 Zurich — Trojan SplitHTTP TLS",
        "routing": f"{DEMO_PREFIX}gaming-low-latency",
        "device_limit": 1,
        "fallback": None,
        "stream": {
            "network": "splithttp",
            "security": "tls",
            "tlsSettings": {
                "serverName": "zur.example.com",
                "alpn": ["h2"],
            },
            "splithttpSettings": {
                "path": "/split",
                "host": "zur.example.com",
            },
        },
    },
    {
        "tag": f"{DEMO_PREFIX}ss-2022-aes",
        "port": 2086,
        "protocol": "shadowsocks",
        "label": "🇸🇬 Singapore — Shadowsocks 2022 (AES-128)",
        "routing": None,
        "device_limit": 0,
        "fallback": None,
        "stream": {
            "network": "tcp",
            "security": "none",
            "ssMethod": "2022-blake3-aes-128-gcm",
            "ssPassword": "J87ix+jNMGJ4Fa7bRkfSGg==",
            "ssNetwork": "tcp",
        },
    },
    {
        
        
        
        
        
        "tag": f"{DEMO_PREFIX}ss-2022-aes256",
        "port": 2087,
        "protocol": "shadowsocks",
        "label": "🇯🇵 Tokyo — Shadowsocks 2022 (AES-256)",
        "routing": None,
        "device_limit": 0,
        "fallback": None,
        "stream": {
            "network": "tcp,udp",
            "security": "none",
            "ssMethod": "2022-blake3-aes-256-gcm",
            "ssPassword": "zh67p2SoGJT/sLv2AMd/9ldXvcAa8o8k5SnD990OaYM=",
            "ssNetwork": "tcp,udp",
        },
    },
    {
        "tag": f"{DEMO_PREFIX}socks-bot",
        "port": 1080,
        "protocol": "socks",
        "label": "🛠️ Internal SOCKS (bot/admin)",
        "routing": None,
        "device_limit": 0,
        "fallback": None,
        "stream": {
            "network": "tcp",
            "security": "none",
            "authUser": "bot",
            "authPass": "demo",
        },
    },
    {
        "tag": f"{DEMO_PREFIX}http-bot",
        "port": 8118,
        "protocol": "http",
        "label": "🛠️ Internal HTTP (bot/admin)",
        "routing": None,
        "device_limit": 0,
        "fallback": None,
        "stream": {
            "network": "tcp",
            "security": "none",
            "authUser": "bot",
            "authPass": "demo",
        },
    },
]



OUTBOUNDS = [
    {
        "tag": f"{DEMO_PREFIX}proxy-eu",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": "eu.upstream.example.com",
                    "port": 443,
                    "users": [
                        {
                            "id": "11111111-2222-3333-4444-555555555555",
                            "encryption": "none",
                        }
                    ],
                }
            ]
        },
        "stream": {"network": "tcp", "security": "tls"},
    },
    {
        "tag": f"{DEMO_PREFIX}proxy-us",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": "us.upstream.example.com",
                    "port": 443,
                    "users": [
                        {
                            "id": "66666666-7777-8888-9999-aaaaaaaaaaaa",
                            "encryption": "none",
                        }
                    ],
                }
            ]
        },
        "stream": {"network": "tcp", "security": "tls"},
    },
    {
        "tag": f"{DEMO_PREFIX}proxy-asia",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": "sg.upstream.example.com",
                    "port": 443,
                    "users": [
                        {
                            "id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
                            "encryption": "none",
                        }
                    ],
                }
            ]
        },
        "stream": {"network": "tcp", "security": "tls"},
    },
    {
        "tag": f"{DEMO_PREFIX}socks-upstream",
        "protocol": "socks",
        "settings": {
            "servers": [
                {
                    "address": "socks.upstream.example.com",
                    "port": 1080,
                    "users": [{"user": "demo", "pass": "demo"}],
                }
            ]
        },
        "stream": {"network": "tcp", "security": "none"},
    },
    {
        "tag": f"{DEMO_PREFIX}warp",
        "protocol": "wireguard",
        "settings": {
            "secretKey": "wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "address": ["172.16.0.2/32"],
            "peers": [
                {
                    "publicKey": "bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=",
                    "endpoint": "engage.cloudflareclient.com:2408",
                    "allowedIPs": ["0.0.0.0/0", "::/0"],
                }
            ],
            "mtu": 1280,
        },
        "stream": {"network": "tcp", "security": "none"},
    },
]



BALANCERS = [
    {
        "tag": f"{DEMO_PREFIX}eu-us-balancer",
        "selector": [f"{DEMO_PREFIX}proxy-eu", f"{DEMO_PREFIX}proxy-us"],
        "strategy": "random",
        "fallback": f"{DEMO_PREFIX}proxy-eu",
    },
    {
        "tag": f"{DEMO_PREFIX}multi-region-leastping",
        "selector": [
            f"{DEMO_PREFIX}proxy-eu",
            f"{DEMO_PREFIX}proxy-us",
            f"{DEMO_PREFIX}proxy-asia",
        ],
        "strategy": "leastPing",
        "fallback": f"{DEMO_PREFIX}proxy-eu",
    },
]



ROUTING_PROFILES = [
    {
        "name": f"{DEMO_PREFIX}ru-direct",
        "rules": [
            {
                "type": "field",
                "enabled": True,
                "comment": "Route .ru and gov domains direct",
                "domain": ["geosite:category-gov-ru", "regexp:.*\\.ru$"],
                "outboundTag": "direct",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "Default → EU-US balancer",
                "network": "tcp,udp",
                "outboundTag": f"{DEMO_PREFIX}eu-us-balancer",
            },
        ],
    },
    {
        "name": f"{DEMO_PREFIX}streaming-balanced",
        "rules": [
            {
                "type": "field",
                "enabled": True,
                "comment": "Streaming → US",
                "domain": [
                    "netflix.com",
                    "googlevideo.com",
                    "youtube.com",
                    "twitch.tv",
                ],
                "outboundTag": f"{DEMO_PREFIX}proxy-us",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "RU sites → direct",
                "domain": ["regexp:.*\\.ru$", "yandex.ru", "vk.com", "kinopoisk.ru"],
                "outboundTag": "direct",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "Default → multi-region least-ping",
                "network": "tcp,udp",
                "outboundTag": f"{DEMO_PREFIX}multi-region-leastping",
            },
        ],
    },
    {
        "name": f"{DEMO_PREFIX}ads-block",
        "rules": [
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
                "comment": "Block private IP ranges",
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
    },
    {
        "name": f"{DEMO_PREFIX}gaming-low-latency",
        "rules": [
            {
                "type": "field",
                "enabled": True,
                "comment": "Gaming traffic → least-ping balancer",
                "domain": [
                    "steamcommunity.com",
                    "steampowered.com",
                    "battle.net",
                    "ea.com",
                    "playstation.com",
                    "xboxlive.com",
                ],
                "outboundTag": f"{DEMO_PREFIX}multi-region-leastping",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "Voice chat → US",
                "domain": ["discord.com", "discordapp.com"],
                "outboundTag": f"{DEMO_PREFIX}proxy-us",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "Default → direct",
                "network": "tcp,udp",
                "outboundTag": "direct",
            },
        ],
    },
    {
        "name": f"{DEMO_PREFIX}work-vpn",
        "rules": [
            {
                "type": "field",
                "enabled": True,
                "comment": "Corp ranges → direct (no proxy needed)",
                "ip": ["10.0.0.0/8", "192.168.0.0/16", "172.16.0.0/12"],
                "outboundTag": "direct",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "Corp SaaS → WARP for stability",
                "domain": [
                    "atlassian.com",
                    "slack.com",
                    "github.com",
                    "notion.so",
                    "1password.com",
                ],
                "outboundTag": f"{DEMO_PREFIX}warp",
            },
            {
                "type": "field",
                "enabled": True,
                "comment": "Default → EU proxy",
                "network": "tcp,udp",
                "outboundTag": f"{DEMO_PREFIX}proxy-eu",
            },
        ],
    },
]














USER_EMAILS = [
    
    ("alice", f"{DEMO_PREFIX}vless-reality-vision"),
    ("bob", f"{DEMO_PREFIX}vless-reality-vision"),
    ("carol", f"{DEMO_PREFIX}vless-reality-vision"),
    ("dave", f"{DEMO_PREFIX}vless-reality-vision"),
    ("eve", f"{DEMO_PREFIX}vless-reality-vision"),
    ("frank", f"{DEMO_PREFIX}vless-reality-vision"),
    ("grace", f"{DEMO_PREFIX}vless-reality-vision"),
    ("hank", f"{DEMO_PREFIX}vless-reality-vision"),
    
    ("iris", f"{DEMO_PREFIX}vless-grpc"),
    ("jack", f"{DEMO_PREFIX}vless-grpc"),
    ("kate", f"{DEMO_PREFIX}vless-grpc"),
    
    ("leo", f"{DEMO_PREFIX}vless-xhttp"),
    ("luna", f"{DEMO_PREFIX}vless-xhttp"),
    
    ("heidi", f"{DEMO_PREFIX}vmess-ws"),
    ("ivan", f"{DEMO_PREFIX}vmess-ws"),
    ("judy", f"{DEMO_PREFIX}vmess-ws"),
    ("kevin", f"{DEMO_PREFIX}vmess-ws"),
    
    ("mallory", f"{DEMO_PREFIX}vmess-httpupgrade"),
    ("niaj", f"{DEMO_PREFIX}vmess-httpupgrade"),
    
    ("olivia", f"{DEMO_PREFIX}trojan-tls"),
    ("peggy", f"{DEMO_PREFIX}trojan-tls"),
    ("ron", f"{DEMO_PREFIX}trojan-tls"),
    
    ("quinn", f"{DEMO_PREFIX}trojan-splithttp"),
    ("ruth", f"{DEMO_PREFIX}trojan-splithttp"),
    
    ("steve", f"{DEMO_PREFIX}ss-2022-aes"),
    ("trent", f"{DEMO_PREFIX}ss-2022-aes"),
    ("uma", f"{DEMO_PREFIX}ss-2022-aes"),
    
    ("victor", f"{DEMO_PREFIX}ss-2022-aes256"),
    ("wendy", f"{DEMO_PREFIX}ss-2022-aes256"),
    
    ("xavier", f"{DEMO_PREFIX}vless-reality-vision"),
    ("yara", f"{DEMO_PREFIX}vmess-ws"),
    ("zoe", f"{DEMO_PREFIX}trojan-tls"),
]


def build_user_profiles():
    
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
        
        
        roll = random.random()
        if archetype == "idle":
            peak = random.randint(300_000, 2_500_000)  
        elif archetype == "sporadic":
            peak = random.randint(8_000_000, 80_000_000)  
        elif roll < 0.10:  
            peak = random.randint(800_000_000, 2_500_000_000)  
        elif roll < 0.35:  
            peak = random.randint(200_000_000, 800_000_000)
        elif roll < 0.75:  
            peak = random.randint(30_000_000, 200_000_000)
        else:  
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
            "burst_chance": random.uniform(
                0.005, 0.04
            ),  
            "asymmetry": random.uniform(0.10, 0.35),  
            "preferred_pool": random.choice(["ru", "ru", "ru", "eu", "asia"]),  
        }
    return profiles


def hour_weight(archetype: str, hour: int, weekday: int) -> float:
    
    if archetype == "evening":
        base = max(0.05, 1.0 - abs(hour - 20) / 8)
    elif archetype == "night-owl":
        
        dist = min(abs(hour - 2), abs(hour - 26)) / 6
        base = max(0.05, 1.0 - dist)
    elif archetype == "day-worker":
        base = max(0.02, 1.0 - abs(hour - 13) / 7)
        if weekday >= 5:
            base *= 0.20
    elif archetype == "all-day":
        
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
    else:  
        base = 0.10
    return base


def hourly_bytes(profile: dict, hour: int, weekday: int) -> tuple[int, int] | None:
    
    if random.random() > profile["online_chance"]:
        return None
    weight = hour_weight(profile["archetype"], hour, weekday)
    if weight <= 0:
        return None
    base = profile["peak"] * weight * random.uniform(0.55, 1.45)
    
    if random.random() < profile["burst_chance"]:
        base *= random.uniform(5.0, 15.0)
    if base < 1024:
        return None
    total = int(base)
    up = int(total * profile["asymmetry"])
    down = total - up
    return up, down



DEMO_IP_POOLS = {
    "ru": [
        "87.117.190.{}",
        "95.31.4.{}",
        "178.140.6.{}",
        "5.182.99.{}",
        "213.87.12.{}",
        "188.123.231.{}",
    ],
    "eu": ["88.99.1.{}", "94.130.45.{}", "159.69.7.{}", "144.76.12.{}", "85.10.200.{}"],
    "us": ["198.51.100.{}", "203.0.113.{}", "192.0.2.{}"],
    "asia": ["210.142.92.{}", "139.99.45.{}", "103.244.50.{}"],
}

DEMO_DOMAINS = [
    "google.com",
    "youtube.com",
    "googlevideo.com",
    "instagram.com",
    "facebook.com",
    "telegram.org",
    "github.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "cloudflare.com",
    "amazon.com",
    "netflix.com",
    "openai.com",
    "anthropic.com",
    "discord.com",
    "spotify.com",
    "reddit.com",
    "wikipedia.org",
    "stackoverflow.com",
    "linkedin.com",
    "pinterest.com",
    "duckduckgo.com",
    "vk.com",
    "yandex.ru",
    "ya.ru",
    "habr.com",
    "lenta.ru",
    "rbc.ru",
    "kinopoisk.ru",
]


def gen_ip(pool: str) -> str:
    return random.choice(DEMO_IP_POOLS[pool]).format(random.randint(1, 254))


def pick_ips(profile: dict) -> list[str]:
    if profile["archetype"] == "idle":
        return [] if random.random() < 0.35 else [gen_ip(profile["preferred_pool"])]
    if profile["archetype"] == "sporadic":
        count = random.randint(1, 3)
    elif profile["peak"] > 500_000_000:  
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





def assign_constraints(name: str, profile: dict, inbound_meta: dict) -> dict:
    
    now_ms = int(datetime.now().timestamp() * 1000)
    day_ms = 86_400_000

    
    enable = random.random() > 0.15

    
    
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
        expiry_time = now_ms - random.randint(1, 7) * day_ms  

    
    if random.random() < 0.30:
        device_limit = random.choice([1, 2, 5])
    else:
        device_limit = 0

    
    reset_day = random.choice([0, 1, 5, 15]) if random.random() < 0.3 else 0

    
    arch = profile["archetype"]
    if arch == "idle":
        last_seen = now_ms - random.randint(2, 60) * day_ms
    elif arch in ("sporadic",):
        last_seen = now_ms - random.randint(1, 14) * day_ms
    elif arch == "day-worker" and datetime.now().weekday() >= 5:
        last_seen = now_ms - random.randint(1, 3) * day_ms
    elif profile["peak"] > 500_000_000:
        last_seen = now_ms - random.randint(0, 20) * 60_000  
    else:
        last_seen = now_ms - random.randint(5, 24 * 60) * 60_000

    
    flow = None
    if (
        inbound_meta["protocol"] == "vless"
        and inbound_meta["stream"].get("security") == "reality"
    ):
        flow = "xtls-rprx-vision" if random.random() < 0.7 else None

    return {
        "limit_bytes": limit_bytes,
        "expiry_time": expiry_time,
        "device_limit": device_limit if device_limit else None,
        "reset_day": reset_day,
        "last_seen": last_seen,
        "enable": enable,
        "flow": flow,
    }





def wipe_existing_demo():
    
    print("→ Wiping existing demo rows...")
    pattern = f"{DEMO_PREFIX}%"
    demo_emails = [f"{n}@vpn" for (n, _) in USER_EMAILS]

    DomainStat.query.filter(DomainStat.inbound_tag.like(pattern)).delete(
        synchronize_session=False
    )
    TrafficSnapshot.query.filter(
        TrafficSnapshot.inbound_tag.like(pattern)
        | (
            (TrafficSnapshot.entity_type == "inbound")
            & (TrafficSnapshot.entity_id.like(pattern))
        )
    ).delete(synchronize_session=False)
    Client.query.filter(Client.inbound_tag.like(pattern)).delete(
        synchronize_session=False
    )
    Inbound.query.filter(Inbound.tag.like(pattern)).delete(synchronize_session=False)
    Balancer.query.filter(Balancer.tag.like(pattern)).delete(synchronize_session=False)
    Outbound.query.filter(Outbound.tag.like(pattern)).delete(synchronize_session=False)
    RoutingProfile.query.filter(RoutingProfile.name.like(pattern)).delete(
        synchronize_session=False
    )
    db.session.commit()


def create_outbounds_balancers_routing():
    print("→ Creating outbounds...")
    for ob in OUTBOUNDS:
        db.session.add(
            Outbound(
                tag=ob["tag"],
                protocol=ob["protocol"],
                enable=True,
                settings=json.dumps(ob["settings"]),
                stream_settings=json.dumps(ob["stream"]),
                mux="{}",
            )
        )
    db.session.commit()

    print("→ Creating balancer(s)...")
    for b in BALANCERS:
        db.session.add(
            Balancer(
                tag=b["tag"],
                enable=True,
                selector=json.dumps(b["selector"]),
                strategy=b["strategy"],
                fallback_tag=b["fallback"],
            )
        )
    db.session.commit()

    print("→ Creating routing profiles...")
    for p in ROUTING_PROFILES:
        db.session.add(
            RoutingProfile(
                name=p["name"],
                rules=json.dumps(p["rules"]),
                enable=True,
            )
        )
    db.session.commit()


def create_inbounds():
    print("→ Creating inbounds...")
    profile_map = {p.name: p.id for p in RoutingProfile.query.all()}
    for ib in INBOUNDS:
        db.session.add(
            Inbound(
                tag=ib["tag"],
                port=ib["port"],
                protocol=ib["protocol"],
                stream_settings=json.dumps(ib["stream"]),
                routing_profile_id=profile_map.get(ib["routing"])
                if ib["routing"]
                else None,
                label=ib["label"],
                device_limit=ib["device_limit"],
                fallback_address=ib["fallback"],
                up=0,
                down=0,
            )
        )
    db.session.commit()


def _inbound_meta_by_tag() -> dict[str, dict]:
    return {ib["tag"]: ib for ib in INBOUNDS}


def _generate_client_id(inbound: dict) -> str:
    
    import base64
    import secrets

    if inbound["protocol"] == "shadowsocks":
        method = (inbound.get("stream") or {}).get("ssMethod", "")
        if "aes-128-gcm" in method:
            return base64.b64encode(secrets.token_bytes(16)).decode()
        if "aes-256-gcm" in method:
            return base64.b64encode(secrets.token_bytes(32)).decode()
    return str(uuid.uuid4())


def create_clients(user_profiles) -> dict[tuple[str, str], Client]:
    print("→ Creating clients with diverse constraints...")
    meta = _inbound_meta_by_tag()
    by_key: dict[tuple[str, str], Client] = {}
    for (name, tag), profile in user_profiles.items():
        email = name  
        constraints = assign_constraints(email, profile, meta[tag])
        ips = pick_ips(profile)
        c = Client(
            id=_generate_client_id(meta[tag]),
            email=email,
            inbound_tag=tag,
            limit_bytes=constraints["limit_bytes"],
            expiry_time=constraints["expiry_time"],
            up=0,
            down=0,
            enable=constraints["enable"],
            last_seen=constraints["last_seen"],
            source_ips=json.dumps(ips),
            reset_day=constraints["reset_day"],
            device_limit=constraints["device_limit"],
            flow=constraints["flow"],
        )
        db.session.add(c)
        by_key[(email, tag)] = c
    db.session.commit()
    return by_key


def create_traffic_snapshots(user_profiles, clients_by_key):
    print(
        "→ Generating 90 days of hourly traffic snapshots (per-user unique patterns)..."
    )
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=90)

    user_totals = {k: [0, 0] for k in clients_by_key}
    inbound_totals = {ib["tag"]: [0, 0] for ib in INBOUNDS}

    rows_user = []
    rows_inbound = []
    cur = start
    while cur <= now:
        bucket_ts = int(cur.timestamp())
        weekday = cur.weekday()
        hour = cur.hour
        per_inbound_up = {ib["tag"]: 0 for ib in INBOUNDS}
        per_inbound_down = {ib["tag"]: 0 for ib in INBOUNDS}

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

    print(
        f"   {len(rows_user)} user-rows, {len(rows_inbound)} inbound-rows — bulk insert..."
    )
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
                continue  
            
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

        print("\n─── Seed complete ───")
        print(f"  Routing profiles:   {RoutingProfile.query.count()}")
        print(f"  Outbounds:          {Outbound.query.count()}")
        print(f"  Balancers:          {Balancer.query.count()}")
        print(f"  Inbounds:           {Inbound.query.count()}")
        print(f"  Clients:            {Client.query.count()}")
        print(f"  Traffic snapshots:  {TrafficSnapshot.query.count()}")
        print(f"  Domain stats:       {DomainStat.query.count()}")

        
        clients = Client.query.filter(Client.inbound_tag.like(f"{DEMO_PREFIX}%")).all()
        disabled = sum(1 for c in clients if not c.enable)
        expired = sum(
            1
            for c in clients
            if c.expiry_time and c.expiry_time < int(datetime.now().timestamp() * 1000)
        )
        with_limit = sum(1 for c in clients if c.limit_bytes > 0)
        with_device_lim = sum(1 for c in clients if c.device_limit)
        no_ips = sum(1 for c in clients if c.source_ips == "[]" or not c.source_ips)
        print("\n  Client constraint diversity:")
        print(f"    disabled:               {disabled}/{len(clients)}")
        print(f"    expired:                {expired}/{len(clients)}")
        print(f"    byte limit set:         {with_limit}/{len(clients)}")
        print(f"    device limit set:       {with_device_lim}/{len(clients)}")
        print(f"    empty source_ips:       {no_ips}/{len(clients)}")

        
        print("\n  Per-inbound client count:")
        for ib in INBOUNDS:
            n = sum(1 for c in clients if c.inbound_tag == ib["tag"])
            print(f"    {ib['label']:<55} → {n}")


if __name__ == "__main__":
    main()
