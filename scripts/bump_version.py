#!/usr/bin/env python3
"""
Bump service versions and update .env.example with new image tags.

Usage:
  bump_version.py patch backend frontend   # patch bump specific services
  bump_version.py minor all               # minor bump all services
  bump_version.py major all               # major bump all services
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
VERSIONS_FILE = ROOT / "versions.json"
ENV_EXAMPLE = ROOT / ".env.example"

IMAGE_KEYS = {
    "backend": "BACKEND_IMAGE",
    "frontend": "FRONTEND_IMAGE",
    "caddy": "CADDY_IMAGE",
    "bot": "BOT_IMAGE",
}

IMAGE_NAMES = {
    "backend": "ghcr.io/ivantopgaming/panel-backend",
    "frontend": "ghcr.io/ivantopgaming/panel-frontend",
    "caddy": "ghcr.io/ivantopgaming/panel-caddy",
    "bot": "ghcr.io/ivantopgaming/panel-bot",
}

SERVICES = list(IMAGE_KEYS.keys())


def bump(version: str, kind: str) -> str:
    major, minor, patch = map(int, version.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    elif kind == "minor":
        return f"{major}.{minor + 1}.0"
    else:
        return f"{major}.{minor}.{patch + 1}"


def update_env_example(data: dict) -> None:
    content = ENV_EXAMPLE.read_text()
    for svc, key in IMAGE_KEYS.items():
        if svc in data:
            image = f"{IMAGE_NAMES[svc]}:v{data[svc]}"
            content = re.sub(
                rf"^{re.escape(key)}=.*$",
                f"{key}={image}",
                content,
                flags=re.MULTILINE,
            )
    ENV_EXAMPLE.write_text(content)


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    kind = sys.argv[1]
    services = sys.argv[2:]

    if kind not in ("patch", "minor", "major"):
        print(f"Unknown bump type: {kind}. Use patch, minor, or major.")
        sys.exit(1)

    data = json.loads(VERSIONS_FILE.read_text())

    if services == ["all"]:
        services = SERVICES

    changed = False
    for svc in services:
        if svc not in SERVICES:
            print(f"Unknown service: {svc}. Valid: {', '.join(SERVICES)}")
            sys.exit(1)
        old = data[svc]
        data[svc] = bump(old, kind)
        print(f"  {svc}: {old} -> {data[svc]}")
        changed = True

    if changed:
        VERSIONS_FILE.write_text(json.dumps(data, indent=2) + "\n")
        update_env_example(data)
        print("Updated versions.json and .env.example")


if __name__ == "__main__":
    main()
