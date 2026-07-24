<div align="center">

# ITG Xray Panel

### A self-hosted [Xray-core](https://github.com/XTLS/Xray-core) manager — with a Telegram billing bot built in

Run a complete VPN service from one place: manage inbounds, users, traffic limits,
routing and live statistics from a modern web UI — and **sell, gift, or auto-renew
subscriptions** straight inside Telegram, with YooKassa payments.

<br/>

[![CI](https://github.com/IvanTopGaming/ITG_xray_panel/actions/workflows/ci.yml/badge.svg)](https://github.com/IvanTopGaming/ITG_xray_panel/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-8b5cf6.svg)](LICENSE) ![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg) ![React](https://img.shields.io/badge/React-18-61DAFB.svg) ![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6.svg) ![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg) ![Redis](https://img.shields.io/badge/Redis-7-DC382D.svg) ![Caddy](https://img.shields.io/badge/Caddy-reverse_proxy-1F88C0.svg)

<br/>

[**Quick Start**](#-quick-start) · [Features](#-features) · [Architecture](#-architecture) · [Configuration](#-configuration) · [Development](#-development)

</div>

---

## Why ITG Xray Panel

- **One stack, end to end** — proxy engine, admin panel, subscription delivery and a paid Telegram bot, wired together and deployed with a single `docker compose`.
- **Sell access without glue code** — tariffs, trials, grants, auto-renewal, expiry/traffic notifications and YooKassa checkout are first-class, not bolted on.
- **Scales horizontally** — a master panel federates any number of remote panels, routing users to specific regions while the bot only ever talks to the master.
- **Built to hide** — the panel lives behind a secret URL, the public domain masquerades as a decoy site, and the Docker socket is locked down to the exact ops the backend needs.

> ⚠️ For lawful use only — run it on infrastructure you own and operate.

---

## 📸 Screenshots

<div align="center">

<img src="docs/shot-01-dashboard.webp" width="760" alt="Dashboard — inbounds, users, live status and traffic"><br/>
<sub><b>Dashboard</b> — inbounds, users, live status &amp; per-user traffic</sub>

<br/><br/>

<img src="docs/shot-02-statistics.webp" width="760" alt="Statistics — traffic over time, top users and sites"><br/>
<sub><b>Statistics</b> — traffic over time, top users &amp; sites</sub>

<br/><br/>

<img src="docs/shot-03-bot.webp" width="760" alt="Telegram bot — tariffs and plans"><br/>
<sub><b>Telegram bot</b> — tariffs &amp; plans</sub>

<br/><br/>

<img src="docs/shot-04-payments.webp" width="760" alt="Payments — YooKassa billing history"><br/>
<sub><b>Payments</b> — YooKassa billing history</sub>

</div>

---

## ✨ Features

### 🧩 Proxy panel

- **Every modern protocol** — VLESS (XTLS Vision · REALITY · TCP · WebSocket · gRPC · XHTTP · HTTPUpgrade), VMess, Trojan, Shadowsocks 2022, WireGuard, SOCKS5, HTTP. Vision flow is kept consistent with its transport automatically — only valid on raw-TCP + TLS/REALITY, and cleared from an inbound's users if you switch it to something incompatible.
- **Live user management** — add/edit users over Xray's gRPC API with no restart for VLESS/VMess; status at a glance (online · offline · expired · over-limit · disabled), last-seen and source-IP tracking, filtering and search.
- **Bulk actions** — select users across inbounds **and** linked panels, then delete / enable / disable / reset traffic / shift expiry / adjust the traffic cap / toggle Vision flow in one shot. Cross-panel batches are proxied to the owning panel and report any unreachable one without aborting the rest.
- **Traffic statistics** — hourly snapshots kept indefinitely, charts with period filtering (1h → all-time), top destination domains, per-panel breakdown.
- **Routing** — outbound servers, weighted balancers with fallback, per-user route overrides.
- **Dedicated egress IP** — send one client's traffic out through a rented secondary IP on the same host. A freedom outbound binds an internal source IP (`sendThrough`), a privileged sidecar keeps that IP aliased inside Xray's netns across restarts, and a panel-generated host script wires the public alias + a self-cleaning SNAT chain (+ policy routing). Assign it to a user with the existing per-user route override; the backend stays fully isolated and only serves the data.
- **Device limits** — optional per-client / per-inbound device cap with HWID-aware subscription delivery.

### 🤝 Subscriptions & Federation

- **Aggregated subscription link** — one URL returns the user's keys merged from the master and every linked panel, Redis-cached with a configurable refresh interval. The link is UUID-keyed, so renaming a user never breaks their app config.
- **Clean subscription domain** — serve subscriptions from a dedicated `SUB_DOMAIN` (e.g. `sub.example.com/...`) instead of the long secret-path URL.
- **Panel Federation** — one master manages any number of remote panels. Route a tariff item to a specific panel via `panel_id`, health-poll every linked panel, and authenticate both directions with a shared federation token.

### 💳 Telegram billing bot

- **YooKassa payments** — full checkout inside the chat. The unsigned webhook is only a trigger: the handler re-fetches the authoritative status from YooKassa before provisioning, backed by a 30-second poll fallback and atomic double-provision protection.
- **Tariffs** — flexible plans with multiple inbound items (e.g. _"EU 100 GB + RU 50 GB / 30 days"_), `public` / `private` / `archived` visibility, optional per-item panel routing.
- **Grants & lifecycle** — admin grants (paid · gift · free), one-time trials, free-tier auto-renewal, and revocation that propagates to linked panels.
- **Smart notifications** — 3-day / 1-day / 1-hour / expired warnings and 80% / 95% / exhausted traffic warnings, deduplicated per cycle and re-armed on monthly reset or renewal.
- **Fully editable copy** — every user-visible string lives in an admin-editable table (RU + EN seeded, add any language by adding rows).
- **Resilient by design** — events are dual-written to Redis pub/sub **and** a recovery table, so a transient Redis outage can't lose a notification. Change the bot token or proxy from the panel and the aiogram session rebuilds in place — no restart.

### 🛡️ Operations & security

- **TLS via Let's Encrypt** — a one-command script issues a SAN certificate for your panel (and subscription) domain; Caddy serves it and redirects `:80 → :443`.
- **Hidden behind a secret path** — everything outside `/<PANEL_SECRET_PATH>/` returns `404`; the bare domain serves a decoy.
- **Instant session kill** — changing the admin password immediately invalidates every active JWT.
- **Locked-down Docker socket** — only the exact container ops the backend needs are exposed, via `tecnativa/docker-socket-proxy`.
- **Backup & restore** — admin-only DB snapshot export/import.

---

## 🛠️ Stack

| Layer           | Technology                                                                                 |
| --------------- | ------------------------------------------------------------------------------------------ |
| Proxy engine    | **Xray-core** — live gRPC user management + JSON config on disk                            |
| Backend         | **Python 3.12 · Flask · gunicorn + gevent · SQLAlchemy · SQLite**                          |
| Frontend        | **React 18 · TypeScript · Vite · Tailwind CSS · Framer Motion · TanStack Query · Zustand** |
| Bot             | **Python 3.12 · Aiogram 3** (asyncio)                                                      |
| Reverse proxy   | **Caddy** — SNI routing, TLS termination, decoy masquerade                                 |
| Cache + pub/sub | **Redis 7**                                                                                |
| Payments        | **YooKassa SDK**                                                                           |
| Orchestration   | **Docker Compose**                                                                         |

---

## 🚀 Quick Start

### 1 · Bootstrap the deployment

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/IvanTopGaming/ITG_xray_panel/main/scripts/install_prod.sh)
```

Downloads `docker-compose.yml`, `caddy/routes.yaml`, the cert helper and `.env.example`, then generates a fresh `.env` with strong random secrets (and prints the admin password + secret path once).

### 2 · Point DNS and edit `.env`

Make `PANEL_DOMAIN` (and `SUB_DOMAIN`, if you use one) resolve to the server, then set:

```env
PANEL_DOMAIN=panel.example.com
SUB_DOMAIN=sub.example.com         # optional — clean subscription URLs
PROXY_DOMAIN=www.google.com        # decoy shown to anything that isn't the panel/sub
PANEL_SECRET_PATH=my-secret-path   # the panel is only reachable at /<this>/
CORS_ORIGINS=https://panel.example.com
```

### 3 · Bring it up

```bash
docker compose pull
docker compose up -d backend frontend redis xray socket-proxy

# Issue the TLS cert and start Caddy (needs certbot + DNS pointing here):
bash scripts/generate_certs.sh
```

Open **`https://panel.example.com/my-secret-path/`** and log in. Everything else returns `404`; the bare domain serves `PROXY_DOMAIN`.

> 💡 **Local / dev?** Use `bash scripts/generate_local_cert.sh` for a self-signed cert instead.

### 4 · Add the Telegram bot _(optional)_

The bot is **integrated with the panel** — all user state (Telegram IDs, languages, notifications, payments) lives in the panel's database, and one bot token may only long-poll once, so run the `bot` service against a **single master** panel.

In **Bot → Settings**, set your `@BotFather` token, admin Telegram IDs, (optionally) YooKassa `shop_id` + `secret_key`, and a display timezone. Then:

```bash
docker compose up -d bot
```

New settings are picked up within ~60 s — no restart needed.

---

## 🏗️ Architecture

```text
   Internet
      │
      ▼
   Caddy  ·  :80 → :443  ·  SNI routing
      │
      ├──►  proxy / decoy domain  ──►  Xray-core  ·  raw TCP · REALITY · live gRPC users
      │
      ▼
   Nginx  ·  static SPA, serves /<secret>/ and /api/sub
      │
      ▼
   Flask API  ·  gunicorn + gevent
      │
      ├──►  SQLite          ·  panel.db
      ├──►  Redis           ·  cache · pub/sub · rate-limit
      ├──►  Linked panels   ·  federation HTTP
      └──►  Telegram bot    ·  aiogram  ──►  Telegram API · YooKassa  (webhook ──► Flask)
```

<details>
<summary><b>Docker services</b></summary>

| Service        | Role                                                                                |
| -------------- | ----------------------------------------------------------------------------------- |
| `xray`         | Xray-core proxy engine — JSON config on disk + live gRPC user management            |
| `backend`      | Flask API, APScheduler crons, DB migrations                                         |
| `frontend`     | React app served by Nginx, mounted under `PANEL_SECRET_PATH`                        |
| `caddy`        | Reverse proxy — SNI routing, TLS termination, `:80→:443` redirect, decoy masquerade |
| `redis`        | Rate limiter, subscription cache, bot pub/sub channel                               |
| `socket-proxy` | Locked-down Docker socket (only the ops `backend` needs)                            |
| `bot`          | Aiogram Telegram bot — runs on the master only                                      |
| `xray-egress`  | Optional sidecar (opt-in via `--profile egress`) that keeps dedicated-egress bind-IPs aliased in Xray's netns |

Networks are split for isolation: `panel-net` (fixed `172.28.0.0/24`, the only segment with internet egress) plus two `internal: true` segments — `redis-net` (backend ↔ redis ↔ bot) and `dockersock-net` (backend ↔ socket-proxy) — so the Docker-socket proxy is reachable only by `backend`, and neither it nor Redis can reach the internet.

</details>

<details>
<summary><b>Background jobs</b> (APScheduler, in the <code>backend</code> container)</summary>

| Job                             | Interval | What it does                                                               |
| ------------------------------- | -------- | -------------------------------------------------------------------------- |
| `sync_traffic`                  | 10s      | Per-user up/down from Xray gRPC → `client` + hourly `traffic_snapshot`     |
| `check_limits`                  | 60s      | Removes users over their limit or past expiry                              |
| `parse_logs`                    | 15s      | Streams Xray access logs into `domain_stat` (top-sites tab)                |
| `cleanup_stats`                 | 24h      | Prunes `domain_stat` to 90 days                                            |
| `poll_linked_panels`            | 10s      | Health-polls each linked panel                                             |
| `auto_renew_free_users`         | 15m      | Re-provisions due free grants; pauses + notifies on tariff archive/disable |
| `poll_pending_payments`         | 30s      | Webhook fallback — reconciles unsettled YooKassa payments                  |
| `cleanup_old_payments`          | 24h      | Cancels stuck pendings, prunes terminal records > 90d                      |
| `send_expiry_notifications`     | 15m      | 3d / 1d / 1h / expired warnings                                            |
| `send_traffic_notifications`    | 15m      | 80% / 95% / exhausted warnings                                             |
| `replay_undelivered_bot_events` | 60s      | Re-publishes any event Redis didn't deliver                                |
| `cleanup_bot_events`            | 24h      | Prunes delivered events > 7d, undelivered > 30d                            |
| `check_latest_version`          | 6h       | Powers the "update available" indicator on **System → About**              |

</details>

<details>
<summary><b>Database</b></summary>

SQLite at `./db_data/panel.db`, ~20 tables, with a custom schema-versioned migration system (`backend/db_migration.py`) that runs idempotently on every backend startup. Storage stays small: `traffic_snapshot` is ~100 bytes per entity per hour, `domain_stat` is capped at 90 days, and bot events at 7d/30d. `GET /api/backup` streams a consistent SQLite snapshot (admin JWT only); `POST /api/restore` swaps one back in.

</details>

---

## ⚙️ Configuration

`.env` drives the deployment; everything bot-specific is managed in the panel UI (**Bot → Settings**) and applied within ~60 s without a restart.

<details>
<summary><b><code>.env</code> reference</b></summary>

**Required**

| Variable               | Description                                             |
| ---------------------- | ------------------------------------------------------- |
| `PANEL_DOMAIN`         | Domain serving the panel                                |
| `PROXY_DOMAIN`         | Decoy site shown for non-panel traffic                  |
| `PANEL_SECRET_PATH`    | URL prefix that gates the panel (everything else → 404) |
| `SECRET_KEY`           | JWT signing key — ≥ 32 chars in production              |
| `PANEL_ADMIN_PASSWORD` | Admin password — non-default in production              |

**Optional**

| Variable                | Default                   | Description                                                  |
| ----------------------- | ------------------------- | ------------------------------------------------------------ |
| `SUB_DOMAIN`            | empty                     | Dedicated subscription domain; clean `https://sub/...` links |
| `PANEL_ADMIN_USER`      | `admin`                   | Admin username                                               |
| `RATELIMIT_STORAGE_URI` | `redis://redis:6379/0`    | Redis URI (`memory://` only for local domains)               |
| `CORS_ORIGINS`          | `https://${PANEL_DOMAIN}` | Comma-separated allowed origins                              |
| `TELEGRAM_PROXY_URL`    | empty                     | HTTP/SOCKS5 proxy the bot uses to reach Telegram             |
| `EGRESS_INTERNAL_TOKEN` | empty                     | Shared token between `backend` and the `xray-egress` sidecar (only for dedicated egress IP) |
| `EGRESS_UPLINK_IFACE`   | `eth0`                    | Host uplink NIC baked into the generated egress host-script  |
| `*_IMAGE`               | from `versions.json`      | Image pins for each service                                  |

> **Local vs production gating.** When `PANEL_DOMAIN` is `localhost`, `*.local`, or a literal IP, the validator relaxes (weak `SECRET_KEY`, default `admin:admin`, and `memory://` are allowed). For any real domain all three are enforced on startup, and the backend refuses to start if a check fails.

</details>

---

## 🔐 TLS & certificates

Caddy serves a certificate from `./certs` — it does **not** use ACME automatically (it owns `:443` for SNI routing, and `:80` is just a redirect). Certificates are issued by a helper script:

```bash
bash scripts/generate_certs.sh
```

It stops Caddy, issues a Let's Encrypt **SAN cert** for `PANEL_DOMAIN` (+ `SUB_DOMAIN` if set) over the standalone challenge on the freed `:80`, installs it into `./certs`, and brings Caddy back. **Renewal is the same command** — re-run it before the 90-day expiry (no cron; you stay in control). For local domains, `scripts/generate_local_cert.sh` writes a self-signed cert instead.

---

## 🌐 Dedicated egress IP

Give a specific client its own public IP without a second server — rent a **secondary IP** on the same host and route that user through it. The panel is the source of truth; the host networking is applied once by a generated script (the `backend` never touches the host).

1. Attach the rented IP to the VM, set `EGRESS_INTERNAL_TOKEN` (+ `EGRESS_UPLINK_IFACE` if your NIC isn't `eth0`) in `.env`, and bring the stack up **with the sidecar**: `docker compose --profile egress up -d`. The sidecar is opt-in, so existing deployments are unaffected.
2. **Routing → Outbounds → New (freedom)** → enter the **Public IP** (and **Gateway** if the IP is on a separate gateway). The internal bind-IP is auto-assigned.
3. **System → Download host script** → run it on the host as root (`bash egress-setup.sh`). Idempotent — re-run it after any egress change. It aliases the public IP and installs a self-cleaning `EGRESS_SNAT` chain (+ policy routing).
4. Assign the client's per-user route (`preferred_outbound`) to that outbound. Verify with `curl https://ifconfig.me` from the client — it shows the dedicated IP, while everyone else still exits the primary one.

> First upgrade to a build with this feature recreates `panel-net` on its fixed `172.28.0.0/24` subnet, so it needs a one-time `docker compose down && up -d` (brief downtime) rather than a plain `up -d`.

---

## 🔌 Protocols

| Protocol             | Notes                                                                                                                                        |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| **VLESS**            | XTLS Vision · REALITY · TCP · WebSocket · gRPC · XHTTP · HTTPUpgrade — the default modern choice. Vision flow requires raw-TCP + TLS/REALITY |
| **VMess**            | Full stream-settings support                                                                                                                 |
| **Trojan**           | TLS required                                                                                                                                 |
| **Shadowsocks 2022** | AES-128-GCM · AES-256-GCM · ChaCha20-Poly1305 (base64 keys of the correct length)                                                            |
| **WireGuard**        | Inbound only                                                                                                                                 |
| **SOCKS5 / HTTP**    | Username/password auth, no panel users                                                                                                       |

Stream settings are stored as one JSON blob per inbound; UI-only keys (`ssMethod`, `wgSecretKey`, …) are stripped before reaching Xray.

---

## 🧪 Development

```bash
# Backend
cd backend && uv sync
uv run python run.py           # dev server :5000
uvx ruff check backend/ tg_bot/    # lint     (CI uses ruff format --check)
uv run pytest tests/               # 970+ unit + API tests

# Frontend
cd frontend && npm install
npm run dev                    # dev server :4200, proxies /api → :5000
npm run build                  # tsc + vite build
npm run lint && npm run format:check

# Bot
cd tg_bot && uv sync
uv run python main.py
```

Rebuild a single service after changes:

```bash
docker compose build backend && docker compose up -d backend
```

`backend/tests/conftest.py` stubs the gRPC modules, so the suite runs on a plain checkout without the Xray protobuf bundle that ships only inside the Docker image.

<details>
<summary><b>CI checks &amp; release pipeline</b></summary>

Every push runs: `ruff check` + `ruff format --check` (backend & bot), `tsc --noEmit`, ESLint, Prettier, the frontend build, the backend test suite, and hadolint. All must pass before code reaches `main`.

Releases are driven entirely by **`versions.json` on `main`**: bump the services you want to ship, mirror the pins in `.env.example`, and merge. CI diffs `versions.json` against the previous commit and builds **only the services whose version changed**. See `CLAUDE.md` for the full workflow, including the federation deploy-ordering rule (deploy the master and every linked panel in the same wave whenever the DB schema version changes).

</details>

---

## 📄 License

[MIT](LICENSE)

<div align="center"><sub>Built for <a href="https://github.com/XTLS/Xray-core">Xray-core</a> · Powered by Docker, Flask, React &amp; Aiogram</sub></div>
