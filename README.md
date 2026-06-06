<div align="center">

# ITG Xray Panel

**Self-hosted management panel + Telegram billing bot for [Xray-core](https://github.com/XTLS/Xray-core)**

[![CI](https://github.com/IvanTopGaming/ITG_xray_panel/actions/workflows/ci.yml/badge.svg)](https://github.com/IvanTopGaming/ITG_xray_panel/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-violet.svg)](LICENSE)

A complete VPN service stack: manage inbounds, users, traffic limits, routing
rules, and real-time statistics from a modern web UI — and sell, gift, or
auto-renew subscriptions through a fully-customisable Telegram bot with
YooKassa payments.

</div>

---

## Highlights

### Proxy panel
- **Multi-protocol** — VLESS (XTLS Vision · Reality · TCP · WebSocket · gRPC · XHTTP · HTTPUpgrade), VMess, Trojan, Shadowsocks 2022, WireGuard, SOCKS5, HTTP. Vision flow is kept consistent with the transport — it's only allowed on raw-TCP + TLS/REALITY, and switching an inbound to e.g. XHTTP clears it from that inbound's users automatically
- **Panel Federation** — one master panel manages linked remote panels: proxy user/inbound CRUD to specific panels via `TariffItem.panel_id` routing, health polling, mutual federation-token auth
- **Aggregated subscriptions** — a single URL returns merged entries from the master and linked panels (Redis-cached, configurable refresh interval)
- **Traffic statistics** — hourly snapshots kept indefinitely, charts, period filtering (1h → all-time), top destination domains, per-panel breakdown
- **Live user status** — online/offline/expired/over-limit/disabled with filtering, last-seen and source-IP tracking
- **Bulk actions** — select users across inbounds *and* linked panels, then delete / enable / disable / reset traffic / shift expiry / adjust traffic cap / toggle VLESS flow in one shot; cross-panel batches proxy to the owning panel and report any unreachable one without aborting the rest
- **Routing** — outbound servers, weighted balancers with fallback, per-user route overrides
- **Device tracking** — optional per-client / per-inbound device limit; HWID-aware subscription delivery
- **Display labels** — admin-friendly inbound names shown to end users (separate from the technical `tag`)

### Telegram billing bot
- **YooKassa payments** — full checkout flow inside Telegram; the unsigned webhook is re-validated against YooKassa's API before provisioning, with a 30-second poll fallback and atomic double-provision protection
- **Tariffs** — flexible plans with multiple inbound items (e.g. "EU 100 GB + RU 50 GB / 30 days"), public/private/archived visibility, optional per-item panel routing
- **Subscription lifecycle** — auto-renewal for free tiers, manual grants by admin (paid · gift · free), revocation
- **Trial** — one-time per-user trial that consumes a dedicated trial tariff
- **Smart notifications** — 3-day / 1-day / 1-hour / expired warnings; 80% / 95% / exhausted traffic warnings; per-cycle dedup with automatic re-arm on monthly reset and tariff renewal
- **i18n** — every user-visible string lives in the admin-editable `bot_text` table; Russian and English seeded by default, custom languages just need their rows
- **Language picker** — on first `/start` the bot asks RU / EN, change later from menu
- **Recovery buffer** — every event is dual-written to Redis pubsub *and* a `bot_event` table so a transient Redis outage doesn't lose notifications (60-second replay cron)
- **Hot-swap token** — change bot token or proxy URL in the panel and the bot re-builds its aiogram session without restarting
- **Telegram proxy** — route the bot's calls through any HTTP/SOCKS5 proxy (handy on RU hosts)

### Operations
- **Panel Federation** — bot only talks to the master panel; the master's `panel_proxy` proxies user CRUD to linked panels as needed
- **Automatic TLS** — Caddy fetches Let's Encrypt certificates, masquerades non-panel paths as a decoy site
- **Hidden panel URL** — everything outside `/<PANEL_SECRET_PATH>/` returns 404
- **Backup + restore** — admin-side DB export/import (admin JWT only)
- **JWT auth with instant invalidation** — change the admin password and all active tokens die immediately (via `pwdv` field)
- **Rate limiting** — Redis-backed limiter on auth endpoints
- **Restricted Docker socket** — only specific container ops exposed through `tecnativa/docker-socket-proxy`

---

## Stack

| Layer | Technology |
|---|---|
| Proxy engine | Xray-core (gRPC-managed live, JSON config on disk) |
| Backend | Python 3.12 · Flask · gunicorn + gevent · SQLAlchemy · SQLite |
| Frontend | React 18 · TypeScript · Vite · Tailwind CSS · Framer Motion · TanStack Query · Zustand |
| Bot | Python 3.12 · Aiogram 3 (asyncio) |
| Reverse proxy | Caddy (automatic TLS, decoy masquerade) |
| Cache + pubsub | Redis 7 |
| Payments | YooKassa SDK ≥ 3.0 |
| Orchestration | Docker Compose |

---

## Quick Start

### 1. Run the install script
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/IvanTopGaming/ITG_xray_panel/main/scripts/install_prod.sh)
```
Downloads `docker-compose.yml`, `caddy/caddy.json`, `.env.example` and creates `.env` from the template.

### 2. Edit `.env`
```env
# Image pins — set to the versions you want to deploy.
XRAY_IMAGE=ghcr.io/xtls/xray-core:v26.3.27
SOCKET_PROXY_IMAGE=tecnativa/docker-socket-proxy:0.3.0
REDIS_IMAGE=redis:7.4-alpine
BACKEND_IMAGE=ghcr.io/ivantopgaming/panel-backend:v2.0.0
FRONTEND_IMAGE=ghcr.io/ivantopgaming/panel-frontend:v2.0.0
CADDY_IMAGE=ghcr.io/ivantopgaming/panel-caddy:v1.0.2
BOT_IMAGE=ghcr.io/ivantopgaming/panel-bot:v2.0.0

PANEL_DOMAIN=panel.example.com
PROXY_DOMAIN=www.google.com               # decoy: shown when someone hits the bare domain
PANEL_SECRET_PATH=my-secret-path          # panel is only reachable at /<this>/
SECRET_KEY=a-very-long-random-string      # ≥ 32 chars in production
PANEL_ADMIN_USER=admin
PANEL_ADMIN_PASSWORD=strong-password
RATELIMIT_STORAGE_URI=redis://redis:6379/0
CORS_ORIGINS=https://panel.example.com
TELEGRAM_PROXY_URL=                       # optional, e.g. socks5://user:pass@1.2.3.4:1080
```

### 3. Start
```bash
docker compose up -d
```
Panel: `https://panel.example.com/my-secret-path/`.
All other paths return 404; the bare domain serves whatever `PROXY_DOMAIN` is set to.

### 4. Set up the bot *(optional)*

The bot is **integrated with the panel**, not standalone — it stores all user state (Telegram IDs, languages, notifications, payments) in the panel's SQLite. Per Telegram-bot rules only one consumer per token may long-poll, so run the `bot` service alongside **one** master panel only.

On the panel, open **Bot → Settings** and set:
- **Bot token** — your `@BotFather` token
- **Admin Telegram IDs** — comma-separated, see `/start` your bot to grab yours
- **Bot service token** — auto-generated; the bot uses this to call the panel API; rotate any time
- **YooKassa** — `shop_id` and `secret_key` if you want paid checkout
- **Display timezone** — used for expiry timestamps in user-facing messages

The bot picks new settings up within ~60 seconds without a restart (runtime-config polling). Changing the token rebuilds the aiogram session in-place.

To run **without** the bot, comment out the `bot:` block in `docker-compose.yml` (or set `replicas: 0` in an override file).

---

## The bot

End-user flow:
1. `/start` → language picker → main menu
2. **My subscription** — list of keys with traffic / expiry, "Show config" QR + URL per server, refresh
3. **Tariffs** — catalog with `[Active]` badges on what the user already owns
4. **Buy** → YooKassa checkout opens in the same chat; on `payment_succeeded` the bot edits the bubble in place and provisions the subscription
5. **Notifications** — payment status, access granted, expiry warnings (3d / 1d / 1h / expired), traffic warnings (80 / 95 / 100%), free-tier auto-renew pause

Admin panel UI (under **Bot** in the side nav):
- **Tariffs** — CRUD with items (one inbound per item, traffic cap in GB, optional panel routing), visibility (`public` / `private` / `archived`), trial flag, drag-sort, archive/restore/duplicate, permanent delete (refused while payments reference it)
- **Users** — every Telegram user the bot has seen, with grants and active clients; per-user `block` / `unblock` (cancels grants, removes from Xray runtime, disables clients, propagates to linked panels)
- **Granted** — `UserTariffAccess` records (the "whitelist" of free / paid / gift grants), revoke per tariff (`vless`/`vmess` removed via gRPC immediately, others trigger config regen + restart)
- **Texts** — every bot string is editable inline, with RU/EN tabs. The bundled `bot_texts_defaults.yaml` ships ~74 keys (RU + EN each) covering the entire user journey. Versioned via `CURRENT_BOT_TEXTS_VERSION`: bumping it triggers a one-shot force-reseed at startup that pushes the new default set
- **Payments** — searchable history (status: pending / processing / succeeded / failed / cancelled), one-click open in YooKassa
- **Settings** — bot token (eye-toggle), admin IDs, bot service token (rotate button), YooKassa credentials, timezone

---

## Architecture

```
                    Internet
                       │
                       ▼
                     Caddy   80 / 443
                       │
        ┌──────────────┼──────────────┐
        │              │              │
        ▼              ▼              ▼
   /<secret>/    /<secret>/api/  everything else
   Nginx           Flask           PROXY_DOMAIN
   (static)         │              (masquerade)
                    ▼
                Flask + gevent + gunicorn
                    │
       ┌────────────┼─────────────┐
       │            │             │
       ▼            ▼             ▼
     Xray         Redis        Linked
     (gRPC)      (cache,        panels
                  pubsub,       (federation
                  rate-limit)    HTTP)
                    ▲
                    │ subscribe bot:events
                    │
                  Telegram bot (aiogram, asyncio)
                    │
                    ▼
                 Telegram API
                    │
                    ▼
                  YooKassa  ──webhook──► Flask  /billing/yookassa/webhook
```

### Docker services
| Service | Role |
|---|---|
| `xray` | Xray-core proxy engine, configured from disk + live gRPC user mgmt |
| `backend` | Flask API + APScheduler crons + DB migrations |
| `frontend` | React app served by Nginx, mounted under `PANEL_SECRET_PATH` |
| `caddy` | Reverse proxy, automatic TLS, decoy masquerade |
| `redis` | Rate limiter, sub-cache, bot pubsub channel |
| `socket-proxy` | Locked-down Docker socket (only the ops `backend` needs) |
| `bot` | Aiogram-based Telegram bot, runs on the master only |

Three Docker networks: `panel-net` (frontend/backend/caddy + Xray + bot — the only network with internet egress) and two `internal: true` segments — `redis-net` (backend ↔ redis ↔ bot) and `dockersock-net` (backend ↔ socket-proxy). Splitting the old `control-net` this way means the Docker-socket proxy is reachable only by `backend`, and neither it nor `redis` can reach the internet.

### Background jobs (APScheduler, all in the `backend` container)

| Job | Interval | What it does |
|---|---|---|
| `sync_traffic` | 10s | Pulls per-user up/down from Xray gRPC, persists to `client` + upserts hourly `traffic_snapshot` rows |
| `check_limits` | 60s | Removes users who exceeded limit or hit expiry |
| `parse_logs` | 15s | Streams Xray access logs, fills `domain_stat` for the top-sites tab |
| `cleanup_stats` | 24h | Prunes `domain_stat` to 90 days |
| `poll_linked_panels` | 10s | Pings each enabled `LinkedPanel`, updates `status` / `last_poll` / `last_error` |
| `auto_renew_free_users` | 15m | Re-provisions free-tier grants whose `next_renewal_at` has passed; pauses + notifies on tariff archive / disable |
| `poll_pending_payments` | 30s | Webhook fallback: reconciles aged-but-unsettled YooKassa payments |
| `cleanup_old_payments` | 24h | Cancels payments stuck `pending > 24h` (with `payment_cancelled` notification) and deletes terminal records `> 90d` |
| `send_expiry_notifications` | 15m | Emits 3-day / 1-day / 1-hour / expired warnings (deduped via `notification_log`, renew button shown only when tariff is still buyable) |
| `send_traffic_notifications` | 15m | Emits 80% / 95% / exhausted warnings (deduped, re-armed on monthly reset and on tariff renewal) |
| `replay_undelivered_bot_events` | 60s | Re-publishes any `bot_event` row with `delivered_at IS NULL` and `created_at < now - 30s` so a transient Redis outage doesn't lose events |
| `cleanup_bot_events` | 24h | Prunes delivered events > 7d, undelivered > 30d |
| `check_latest_version` | 6h | Fetches the published `versions.json` from GitHub and caches it to power the "update available" indicator on **System → About** |

### Database

SQLite at `./db_data/panel.db`, custom in-app migration system (`backend/db_migration.py`) keyed off `PRAGMA user_version` — current schema **v17**, 20 tables. Migrations are idempotent and run on every backend startup.

**Storage budget:**
- `traffic_snapshot` ≈ 100 bytes × entities × 8760 hours/year — negligible for typical deployments
- `domain_stat` capped at 90 days
- `bot_event` capped at 7 days delivered / 30 days undelivered

**Backups:** `GET /api/backup` (admin JWT only) streams a SQLite Backup-API snapshot that includes WAL. `POST /api/restore` swaps a backup back in and triggers a worker restart.

### Panel Federation

Run a second (or third...) panel exactly the same way as the master, then on the master:
**Panels --> Add** with the remote panel's URL and a shared federation token. The remote panel stores the master's credentials in its `FederationConfig` singleton.

The master proxies user/inbound CRUD to linked panels via `panel_proxy.py`. Tariff items can optionally specify a `panel_id` to route provisioning to a specific linked panel instead of the local instance.

The `poll_linked_panels` job (10s) monitors each linked panel's health, updating its `status`, `last_poll`, and `last_error` fields.

Subscription links served by the master can merge entries from linked panels visible to the requesting user, cached in Redis.

---

## Configuration reference

### `.env` — required
| Variable | Description |
|---|---|
| `PANEL_DOMAIN` | Domain serving the panel |
| `PROXY_DOMAIN` | Decoy site shown for non-panel paths |
| `PANEL_SECRET_PATH` | URL prefix that gates the panel (everything else → 404) |
| `SECRET_KEY` | JWT signing key — ≥ 32 chars in production |
| `PANEL_ADMIN_PASSWORD` | Admin password — non-default in production |

### `.env` — optional
| Variable | Default | Description |
|---|---|---|
| `PANEL_ADMIN_USER` | `admin` | Admin username |
| `RATELIMIT_STORAGE_URI` | `redis://redis:6379/0` | Redis URI; `memory://` only allowed for local domains |
| `CORS_ORIGINS` | `https://${PANEL_DOMAIN}` | Comma-separated allowed origins |
| `TELEGRAM_PROXY_URL` | empty | HTTP/HTTPS/SOCKS5 URL the bot uses to reach `api.telegram.org` |
| `XRAY_CORE_REF` | from `versions.json` | Xray-core git tag/SHA used when building the backend image from source |

> **Local vs production gating.** When `PANEL_DOMAIN` is `localhost`, `*.local`, or a literal IP, the validator relaxes: weak `SECRET_KEY` is allowed, default `admin:admin` credentials are allowed, `memory://` rate limiting is allowed. For any real domain all three are enforced on startup and the backend refuses to start if a check fails.

### Bot configuration

The bot reads configuration **from the panel** (not a YAML on disk). At startup it bootstraps a `runtime_config` from `GET /api/bot/runtime-config` and re-polls every 60 seconds. Required pieces (set under **Bot → Settings**):

- `bot_token` — Telegram BotFather token
- `admin_telegram_ids` — comma-separated list of Telegram user IDs
- `bot_service_token` — auto-generated, rotatable; this is what `BOT_SERVICE_TOKEN` env var on the bot container is set to
- YooKassa `shop_id` + `secret_key` (optional, gates the checkout flow)
- `display_timezone` (IANA name) — used to format expiry timestamps

The bot container only needs two env vars:
- `BACKEND_API_URL` — `http://backend:5000/api` in compose
- `BOT_SERVICE_TOKEN` — value of the corresponding `SystemSetting`

---

## Updating

```bash
# Pull new images as defined in .env
docker compose pull

# Restart changed services (zero downtime for unchanged ones)
docker compose up -d

# Clean up old images
docker image prune -f
```

The backend runs DB migrations on startup. To verify nothing is stuck, check the logs:
```bash
docker logs --tail 50 panel-backend | grep "DB migration complete"
```

---

## Protocols

| Protocol | Notes |
|---|---|
| VLESS | XTLS Vision, Reality, TCP, WebSocket, gRPC, XHTTP, HTTPUpgrade — the default modern choice. Vision flow requires raw-TCP + TLS/REALITY |
| VMess | Full stream-settings support |
| Trojan | TLS required |
| Shadowsocks 2022 | AES-128-GCM · AES-256-GCM · ChaCha20-Poly1305 (base64 keys of the right byte length) |
| WireGuard | Inbound only |
| SOCKS5 / HTTP | Username/password auth, no panel users |

Stream settings (TLS, Reality, WS path, etc.) are stored as a single JSON blob per inbound. The blob can carry extra UI-only keys (`ssMethod`, `ssPassword`, `authUser`, `wgSecretKey`, etc.); those are stripped before being handed to Xray.

---

## Security

- Panel is only reachable at `/<PANEL_SECRET_PATH>/` — everything else 404s
- Changing the admin password immediately invalidates all active sessions (JWT `pwdv` field)
- Docker socket is restricted via `socket-proxy` to the exact container ops the backend needs
- Rate limiting on auth endpoints via Redis
- JWT tokens expire after 2 hours
- Bot service token uses constant-time comparison (`secrets.compare_digest`)
- YooKassa webhook is **unsigned**, so the body is only a trigger: the handler re-fetches the authoritative payment status from YooKassa's API before provisioning, making forged notifications harmless (no IP whitelist needed)
- `/api/backup` and `/api/restore` require an admin JWT — the bot service token is **not** accepted there

---

## Development

```bash
# Backend
cd backend && pip install -r requirements.txt
python run.py                  # dev server :5000
python db_migration.py         # standalone migration

ruff check backend/            # lint
ruff format backend/           # autoformat (CI checks --check mode)

# Frontend
cd frontend && npm install
npm run dev                    # dev server :4200, proxies /api → :5000
npm run build                  # production build (tsc + vite build)
npm run lint                   # ESLint
npm run format                 # Prettier autoformat
npm run format:check           # CI mode

# Bot
cd tg_bot && pip install -r requirements.txt
python main.py
```

Rebuild a single service after code changes:
```bash
docker compose build backend  && docker compose up -d backend
docker compose build frontend && docker compose up -d frontend
docker compose build bot      && docker compose up -d bot
```

### Backend tests

```bash
cd backend
pip install pytest
pytest tests/                  # 760+ unit + API tests
pytest tests/test_provisioning.py -q
```

`conftest.py` stubs gRPC modules so tests run on a dev checkout without needing the Xray protobuf bundle that ships only in the Docker image.

### CI checks

All checks must pass before code reaches `main`. Run locally before pushing:

| Check | Command |
|---|---|
| Python lint + format | `ruff check backend/ tg_bot/` · `ruff format --check backend/ tg_bot/` |
| TypeScript typecheck | `cd frontend && npx tsc --noEmit` |
| ESLint | `cd frontend && npm run lint` |
| Prettier | `cd frontend && npm run format:check` |
| Frontend build | `cd frontend && npm run build` |
| Dockerfile lint | hadolint (CI only) |

### Release pipeline

Driven entirely by `versions.json` on `main`. Bump the services you want to ship, also update the matching line in `.env.example`, merge to `main`. CI diffs `versions.json` against the previous commit and builds **only the services whose version changed**. See `CLAUDE.md` for the full workflow.

---

## License

[MIT](LICENSE)
