<div align="center">

# ITG Xray Panel

**Self-hosted management panel for [Xray-core](https://github.com/XTLS/Xray-core)**

[![CI](https://github.com/IvanTopGaming/ITG_xray_panel/actions/workflows/ci.yml/badge.svg)](https://github.com/IvanTopGaming/ITG_xray_panel/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-violet.svg)](LICENSE)

Manage inbound/outbound proxies, users, traffic limits, routing rules and real-time statistics — all from a modern web UI and Telegram bot.

</div>

---

## Features

- **Multi-protocol** — VLESS, VMess, Trojan, Shadowsocks 2022, WireGuard, SOCKS5, HTTP
- **User management** — traffic limits, expiry dates, enable/disable, per-user routing
- **Traffic statistics** — hourly snapshots, charts, period filtering (1h → all-time), top sites
- **User status** — live online/offline/expired/over-limit/disabled indicators with filtering
- **Routing** — outbound servers, balancers, per-user route overrides
- **Subscription links** — v2ray URI and Clash YAML per user
- **Telegram bot** — full user management and notifications from Telegram
- **Security** — JWT auth, rate limiting via Redis, hidden panel URL, Docker socket proxy

---

## Stack

| Layer | Technology |
|---|---|
| Proxy engine | Xray-core |
| Backend | Python · Flask · SQLAlchemy · SQLite |
| Frontend | React · TypeScript · Vite · Tailwind CSS · Framer Motion |
| Bot | Python · Aiogram |
| Reverse proxy | Caddy (automatic TLS) |
| Rate limiting | Redis |
| Orchestration | Docker Compose |

---

## Quick Start

### 1. Run the install script

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/IvanTopGaming/ITG_xray_panel/main/scripts/install_prod.sh)
```

This downloads `docker-compose.yml`, `caddy/caddy.json`, `.env.example` and creates `.env` and `bot_config.yaml` from templates.

### 2. Edit `.env`

```env
# Pin all images to a specific release
XRAY_IMAGE=ghcr.io/xtls/xray-core:v25.3.6
SOCKET_PROXY_IMAGE=tecnativa/docker-socket-proxy:0.3.0
REDIS_IMAGE=redis:7.4-alpine
BACKEND_IMAGE=ghcr.io/ivantopgaming/panel-backend:v1.0.0
FRONTEND_IMAGE=ghcr.io/ivantopgaming/panel-frontend:v1.0.0
CADDY_IMAGE=ghcr.io/ivantopgaming/panel-caddy:v1.0.0
BOT_IMAGE=ghcr.io/ivantopgaming/panel-bot:v1.0.0

PANEL_DOMAIN=panel.example.com
PROXY_DOMAIN=www.google.com
PANEL_SECRET_PATH=my-secret-path      # panel is only reachable at this path
SECRET_KEY=a-very-long-random-string
PANEL_ADMIN_USER=admin
PANEL_ADMIN_PASSWORD=strong-password
```

### 3. Edit `bot_config.yaml` *(optional)*

```yaml
bot_token: "YOUR_TELEGRAM_BOT_TOKEN"
admin_ids:
  - 123456789
servers:
  - name: "Main Panel"
    url: "https://panel.example.com/my-secret-path"
    user: "admin"
    password: "strong-password"
    inbound_tag: "master"
```

If you don't use the bot, comment out the `bot` service in `docker-compose.yml`.

### 4. Start

```bash
docker compose up -d
```

The panel is available at `https://panel.example.com/my-secret-path/`. All other paths return 404.

---

## Updating

```bash
# Pull new images defined in .env
docker compose pull

# Restart changed services (zero downtime for unchanged ones)
docker compose up -d

# Clean up old images
docker image prune -f
```

---

## Protocols

| Protocol | Notes |
|---|---|
| VLESS | XTLS, Reality, WebSocket, gRPC, TCP, etc. |
| VMess | Full stream settings support |
| Trojan | TLS required |
| Shadowsocks 2022 | AES-128-GCM · AES-256-GCM · ChaCha20-Poly1305 |
| WireGuard | Inbound only |
| SOCKS5 / HTTP | Username/password auth, no panel users |

---

## Architecture

```
Internet
   │
   ▼
 Caddy  ─── 80/443 ──► routes /secret-path → Nginx (frontend)
                        everything else → PROXY_DOMAIN (masquerade)
   │
   ▼
 Nginx  ─── static assets
            /api → Flask (backend)
   │
   ▼
 Flask  ─── REST API
            APScheduler jobs (traffic sync every 10s, limit checks every 60s)
            gRPC ──► Xray-core  (live user management, traffic counters)
```

| Service | Role |
|---|---|
| `xray` | Xray-core proxy engine |
| `backend` | Flask API + scheduler jobs |
| `frontend` | React app (Nginx) |
| `caddy` | Reverse proxy, automatic TLS |
| `redis` | Rate limiting |
| `socket-proxy` | Restricted Docker socket (containers only) |
| `bot` | Telegram bot |

**Database:** SQLite at `./db_data/`. Traffic snapshots stored hourly per user/inbound indefinitely. Domain stats pruned to 90 days.

---

## Configuration Reference

| Variable | Required | Description |
|---|---|---|
| `PANEL_DOMAIN` | Yes | Domain serving the panel |
| `PROXY_DOMAIN` | Yes | Masquerade domain for non-panel traffic |
| `PANEL_SECRET_PATH` | Yes | URL prefix for the panel |
| `SECRET_KEY` | Yes | JWT signing key |
| `PANEL_ADMIN_USER` | No | Admin username (default: `admin`) |
| `PANEL_ADMIN_PASSWORD` | Yes | Admin password |
| `RATELIMIT_STORAGE_URI` | No | Redis URI (default: `redis://redis:6379/0`) |
| `CORS_ORIGINS` | No | Allowed CORS origins |
| `XRAY_CORE_REF` | Build only | Xray-core git tag for building backend from source |

> **Local/dev mode:** when `PANEL_DOMAIN` is `localhost`, a local IP, or `*.local` — weak secrets and default credentials are allowed.

---

## Development

```bash
# Backend
cd backend && pip install -r requirements.txt
python run.py           # dev server :5000

# Frontend
cd frontend && npm install
npm run dev             # dev server :4200, proxies /api → :5000
npm run build           # production build + TypeScript check
npm run lint            # ESLint
npm run format          # Prettier

# Bot
cd tg_bot && pip install -r requirements.txt
python main.py
```

Rebuild a single service after code changes:

```bash
docker compose build backend  && docker compose up -d backend
docker compose build frontend && docker compose up -d frontend
```

---

## Security

- Panel is only reachable at `/<PANEL_SECRET_PATH>/` — all other paths return 404
- Changing the admin password immediately invalidates all active sessions (JWT `pwdv` field)
- Docker socket is restricted via `socket-proxy` to container read operations only
- Rate limiting on all auth endpoints via Redis
- JWT tokens expire after 2 hours

---

## License

[MIT](LICENSE)
