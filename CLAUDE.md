# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ITG Xray Panel is a full-stack VPN/proxy management panel for the [Xray-core](https://github.com/XTLS/Xray-core) proxy platform. It manages inbound/outbound proxy configurations, user accounts with traffic limits, routing rules, and real-time traffic statistics. A Telegram bot handles notifications and user management.

**Stack:** Python Flask (backend API) · React + TypeScript + Vite (frontend) · SQLite + SQLAlchemy · Xray-core via gRPC · Redis (rate limiting/cache) · Caddy (reverse proxy) · Docker Compose (orchestration)

## Commands

### Docker (primary workflow)
```bash
docker-compose up                              # Start all services (dev)
docker-compose -f docker-compose.prod.yml up  # Production

# Rebuild and restart a single service after code changes:
docker-compose build frontend && docker-compose up -d frontend
docker-compose build backend  && docker-compose up -d backend
```

### Backend (Python/Flask)
```bash
cd backend
pip install -r requirements.txt
python run.py                  # Dev server on :5000
python db_migration.py         # Run DB migrations
```

No Python linting tools are configured (no flake8, ruff, mypy, black).

### Frontend (React/Vite)
```bash
cd frontend
npm install
npm run dev      # Dev server on :4200 (proxies /api → :5000)
npm run build    # Production build — also runs tsc, so use this to typecheck
npm run preview  # Preview production build
```

No ESLint or Prettier is configured. `npm run build` is the only way to catch TypeScript errors.

### Telegram Bot
```bash
cd tg_bot
pip install -r requirements.txt
python main.py
```

## Architecture

### Docker Services
| Service | Role |
|---------|------|
| `xray` | Xray-core proxy engine |
| `backend` | Flask API (gunicorn + gevent, single worker) |
| `frontend` | React app served by Nginx |
| `caddy` | Reverse proxy (ports 80/443) |
| `redis` | Rate limiting + caching |
| `socket-proxy` | Restricts Docker socket access to specific API operations |
| `bot` | Telegram bot |

Key volumes: `shared_config:/etc/xray` (shared between `xray` and `backend`), `xray_logs:/var/log/xray`, `./db_data:/app/db` (SQLite).

### Backend (`backend/`)
- `app/__init__.py` — Flask app factory; registers all blueprints and extensions
- `app/models.py` — SQLAlchemy models: `Admin`, `Inbound`, `Client`, `Outbound`, `RoutingProfile`, `Balancer`, `SystemSetting`, `TrafficSnapshot`, `DomainStat`
- `app/extensions.py` — Shared Flask extensions (db, migrate, APScheduler, Flask-Limiter)
- `app/api/` — REST API blueprints: `auth`, `inbound`, `outbound`, `routing`, `subscription`, `system`, `statistics`
- `app/services/xray.py` — Core service: generates Xray JSON config, communicates with Xray via gRPC (user add/remove, traffic stats, log tailing)
- `app/services/stats.py` — Traffic statistics collection and enforcement (limits, expiry, snapshot saving, domain tracking)
- `app/services/runtime_identity.py` — Generates user identities (UUIDs, keys) for protocols

### Frontend (`frontend/src/`)
- `pages/` — `Dashboard` (inbound/outbound management), `Statistics` (traffic analytics), `Routing`, `System` (settings + logs), `Login`
- `lib/api.ts` — Axios-based API client with auth interceptor (auto-logout on 401)
- `lib/types.ts` — TypeScript interfaces for all API entities
- `lib/protocols.ts` — Protocol and stream settings definitions (VLESS, VMESS, Shadowsocks, etc.)
- `stores/` — Zustand stores for auth and log state
- `components/ui/` — Shared UI primitives; `components/inbound/` — inbound-specific forms

### Telegram Bot (`tg_bot/`)
- `main.py` — Aiogram asyncio bot entry point
- `api_service.py` — HTTP client wrapping the panel's REST API
- `handlers/` — Admin and user message handlers
- `jobs.py` — Scheduled jobs (backups, expiry notifications)
- Bot requires `/app/config.yaml` (not `.env`): `bot_token`, `admin_ids` (array), and `servers` array with `name`, `url`, `user`, `password`, `inbound_tag`. Missing required fields exits on startup.

## Key Concepts

**Xray gRPC integration:** `xray.py` both generates the full Xray JSON config (written to file) and manages live users via the Xray Stats/Handler gRPC API. Config regeneration and Xray restart happen together when inbounds/outbounds change. A file lock (`/etc/xray/config.lock`) serializes concurrent config writes from the scheduler and request handlers.

**Traffic enforcement:** `stats.py` periodically queries Xray gRPC for per-user traffic, updates `Client.up`/`Client.down` in the DB, and removes users who exceed limits or have expired.

**Background scheduler jobs** (APScheduler, defined in `app/__init__.py`):
- `sync_traffic` — every 10s: queries Xray gRPC for per-user stats; also upserts hourly `TrafficSnapshot` rows via raw SQL `ON CONFLICT DO UPDATE`
- `check_limits` — every 60s: removes expired/over-limit users
- `parse_logs` — every 15s: parses Xray access logs; extracts destination hosts into `DomainStat` (skips bare IPs)
- `cleanup_stats` — every 24h: deletes `DomainStat` rows older than 90 days

**Backend error handling pattern:** All API handlers follow the same two-catch pattern. `ValueError` is the type for user-facing validation errors — it propagates as HTTP 400 with the message shown to the user. Bare `Exception` means an unexpected server fault and returns HTTP 500 with a generic message. Always raise `ValueError` (not `Exception`) for input validation failures in `_build_stream_settings` and other service-layer functions so the error reaches the user.

**Stream settings storage pattern:** Inbound stream settings are stored as a single JSON blob in `Inbound.stream_settings`. This blob carries extra keys beyond what Xray understands (`ssMethod`, `ssPassword`, `ssNetwork`, `authUser`, `authPass`, `wgSecretKey`, `wgPublicKey`, `wgMTU`). `generate_config_file()` strips these keys before writing the Xray config. When adding support for a new protocol, follow this pattern: store all protocol metadata in the blob, strip extra keys in the stripping list at the bottom of `generate_config_file()`.

**Protocol/stream types:** Protocol details live in `frontend/lib/protocols.ts` (UI-facing) and are serialized to JSON in backend models. Client IDs must be valid UUIDs for VLESS/VMess/Trojan, valid WireGuard private keys for WireGuard. Shadowsocks 2022 server/user passwords must be base64-encoded keys of the correct byte length (16 bytes for AES-128, 32 bytes for AES-256 and ChaCha20).

**Subscription links:** `api/subscription.py` generates per-user share links (v2ray URI, Clash YAML) from stored inbound/client data.

**Auth:** JWT tokens (2h expiry) carry a `pwdv` (password version) field tied to `Admin.password_changed_at` — changing the admin password instantly invalidates all existing tokens and the frontend calls `logout()` on success. Tokens are persisted in `localStorage` via Zustand's `persist` middleware. A 401 response from any API call triggers automatic logout via the axios interceptor in `lib/api.ts`.

**Custom Select component:** `components/ui/Select.tsx` renders a portal-based dropdown instead of a native `<select>`. It synthesizes a `React.ChangeEvent<HTMLSelectElement>` in its `onChange`. When used with react-hook-form, always spread `{...register('fieldName')}` so the `name` prop is passed — react-hook-form looks up the field by `event.target.name` and silently ignores the change if `name` is missing or empty.

**Default outbounds:** On startup, `direct` (freedom) and `block` (blackhole) outbounds are auto-created if missing. These are always re-enabled if disabled — do not delete them.

**Database migrations:** `db_migration.py` is a custom migration system (not Flask-Migrate). Current schema version is `4`, tracked via `PRAGMA user_version`. It uses `ALTER TABLE` for column additions, `CREATE TABLE IF NOT EXISTS` for new tables (`_ensure_stats_tables`), and handles field renames. Runs automatically on startup; also runnable standalone. When adding a new table, add a `_ensure_<name>_table` function and call it from `migrate_sqlite_db`, then bump `CURRENT_DB_VERSION`.

**Statistics storage:** `TrafficSnapshot` stores hourly traffic deltas per entity (user or inbound) forever — space is ~100 bytes × entities × 8760 hours/year, negligible for typical deployments. `DomainStat` stores daily domain hit counts and is pruned to 90 days. Both use SQLite `ON CONFLICT DO UPDATE` upserts via `literal_column()` + raw `text()` SQL — do not replace with ORM insert, it breaks atomicity.

**Secret path injection:** The frontend is served under `PANEL_SECRET_PATH`. At container startup, `frontend/entrypoint.sh` injects `window.__PANEL_BASE_URL__` into `index.html` and generates `nginx.conf` from `nginx.conf.template`. All traffic outside the secret path returns 404.

**gevent + gRPC:** `grpc_gevent.init_gevent()` is called at app startup before any gRPC usage. The backend runs under gunicorn+gevent (single worker), so gRPC calls must be gevent-compatible (grpcio 1.59.0).

**Frontend tab/slider style:** All horizontal tab bars use a consistent pill style: container `bg-white/[0.04] p-1 rounded-2xl border border-white/[0.05]`, active item is an absolutely-positioned `motion.div` with `layoutId` and `bg-gradient-to-br from-primary/25 to-violet-600/20 rounded-xl border border-white/[0.1] shadow-[0_0_12px_rgba(208,188,255,0.12)]`, spring transition `stiffness: 500, damping: 35`. Do not use plain CSS active classes for tab bars.

## Git Workflow

### Feature branches — always
All work on service code (`backend/`, `frontend/`, `tg_bot/`, `caddy/`) goes in a feature branch, never directly on `main`.

```bash
git checkout -b feat/my-feature   # create branch
# work, commit freely — history doesn't matter here
git checkout main
git merge --squash feat/my-feature
git commit -m "feat(service): concise description"
git push
git branch -d feat/my-feature
```

`--squash` collapses all branch commits into one staged diff. Write one clean commit message, push once — CI runs once, one bump commit appears in `main`.

**Committing directly to `main` is only acceptable for CI/config-only changes** (`.github/`, `scripts/`, `CLAUDE.md`, `docker-compose*.yml`) that don't touch service source files and therefore don't trigger a release.

### CI/CD skip tags
Two tags control the release pipeline:

| Tag | Effect |
|-----|--------|
| `[skip ci]` | GitHub skips **all** workflows — use on auto-commits that must not re-trigger CI (e.g. the version bump commit itself) |
| `[skip release]` | Only the release job is skipped, other workflows still run — use when you push to `main` directly but don't want a new image built (e.g. fixing a typo in docs, restoring `versions.json`) |

Both tags are needed on the same commit only when you push non-triggering paths to `main` and want to be explicit. In practice `[skip ci]` alone is sufficient for most manual `main` commits.

### How the release pipeline works
1. Push to `main` with changes under `backend/`, `frontend/`, `caddy/`, or `tg_bot/`
2. CI detects which service(s) changed via `git diff`
3. Patch-bumps only those services in `versions.json` and `.env.example`
4. Builds and pushes Docker images to GHCR
5. Commits the version bump back to `main` with `[skip ci]`

Force-pushing rewrites history — CI can't diff against the old SHA and falls back to diffing `HEAD~1..HEAD`. Avoid force-pushing `main`; use feature branches so it's never needed.

## Configuration

Copy `.env.example` to `.env`. Key variables:
- `PANEL_DOMAIN`, `PROXY_DOMAIN`, `PANEL_SECRET_PATH` — routing/TLS
- `SECRET_KEY`, `PANEL_ADMIN_USER`, `PANEL_ADMIN_PASSWORD`
- `XRAY_CORE_REF` — Xray-core version to compile into the Docker image
- `RATELIMIT_STORAGE_URI` — Redis URI for rate limiting

**Local vs. production validation:** When `PANEL_DOMAIN` is a local hostname (localhost, *.local, IP), the app relaxes requirements: weak `SECRET_KEY` is allowed, default `admin:admin` credentials are allowed, `memory://` rate limiting is allowed. For any real domain, all three are enforced on startup and the app refuses to start if they fail.
