# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ITG Xray Panel is a full-stack VPN/proxy management panel for the [Xray-core](https://github.com/XTLS/Xray-core) proxy platform. It manages inbound/outbound proxy configurations, user accounts with traffic limits, routing rules, real-time traffic statistics, and a **YooKassa-backed billing system** with a fully customisable Telegram bot.

**Stack:** Python 3.12 · Flask · gunicorn+gevent · SQLAlchemy · SQLite · Xray-core via gRPC · React + TypeScript + Vite · Aiogram 3 · Redis · Caddy · Docker Compose

## Commands

### Docker (primary workflow)
```bash
docker compose up                              # Start all services (dev)
docker compose -f docker-compose.prod.yml up   # Production

# Rebuild and restart a single service after code changes:
docker compose build frontend && docker compose up -d frontend
docker compose build backend  && docker compose up -d backend
docker compose build bot      && docker compose up -d bot
```

### Backend (Python/Flask)
```bash
cd backend
pip install -r requirements.txt
python run.py                  # Dev server on :5000
python db_migration.py         # Run DB migrations standalone

ruff check backend/
ruff format backend/           # auto-fix formatting
ruff format --check backend/   # CI mode — no changes, exit 1 if dirty

pytest tests/                  # 260+ unit + integration tests
```

`backend/tests/conftest.py` stubs gRPC modules in `sys.modules` before importing the app so tests run on a dev checkout without needing the protobuf bundle that ships only inside the Docker image.

### Frontend (React/Vite)
```bash
cd frontend
npm install
npm run dev           # Dev server on :4200 (proxies /api → :5000)
npm run build         # Production build + tsc typecheck
npm run preview       # Preview production build
npm run lint          # ESLint
npm run format:check  # Prettier check (CI mode)
npm run format        # Prettier auto-fix
```

### Telegram Bot
```bash
cd tg_bot
pip install -r requirements.txt
BACKEND_API_URL=http://backend:5000/api BOT_SERVICE_TOKEN=<token> python main.py

ruff check tg_bot/
ruff format tg_bot/
ruff format --check tg_bot/
```

## Architecture

### Docker Services
| Service | Role |
|---|---|
| `xray` | Xray-core proxy engine |
| `backend` | Flask API + APScheduler crons (gunicorn + gevent, single worker) |
| `frontend` | React app served by Nginx |
| `caddy` | Reverse proxy (ports 80/443), automatic TLS, decoy masquerade |
| `redis` | Rate limiting + sub-cache + bot pubsub channel |
| `socket-proxy` | Restricts Docker socket access to specific API ops |
| `bot` | Telegram bot (Aiogram, asyncio) |

Two networks: `panel-net` (frontend/backend/caddy + xray) and `control-net` (backend ↔ socket-proxy ↔ redis ↔ bot). Key volumes: `shared_config:/etc/xray`, `xray_logs:/var/log/xray`, `./db_data:/app/db`.

### Backend (`backend/`)
- `app/__init__.py` — Flask app factory; registers blueprints, extensions, ProxyFix, APScheduler jobs
- `app/models.py` — SQLAlchemy models (20 total). Core: `Admin`, `Inbound`, `Client`, `Outbound`, `RoutingProfile`, `Balancer`, `SystemSetting`, `TrafficSnapshot`, `DomainStat`, `LinkedPanel`, `FederationConfig`, `ClientDevice`. Billing/bot: `Tariff`, `TariffItem`, `UserTariffAccess`, `Payment`, `BotText`, `BotEvent`, `TelegramUser`, `NotificationLog`
- `app/extensions.py` — Shared Flask extensions (db, migrate, APScheduler, Flask-Limiter)
- `app/utils.py` — JWT helpers + auth decorators: `token_required` (admin JWT only), `bot_service_token_required` (bot service token only), `admin_or_bot_token_required` (accepts either), `federation_token_required` (validates federation token from linked panels), `admin_or_federation_token_required` (accepts admin JWT or federation token). The latter two support the Panel Federation system. `admin_or_bot_token_required` is used on `/api/inbound`, `/api/panels`, and most `/api/system` endpoints — **but NOT on `/api/backup` and `/api/restore`** which take admin-only after the ultrareview hardening.
- `app/api/`
  - `auth` — login / logout
  - `inbound`, `outbound`, `routing`, `panels`, `federation`, `subscription`, `statistics`, `system` — core panel
  - `billing` — YooKassa checkout + IP-whitelisted webhook
  - `bot_admin` — admin UI endpoints (tariffs, texts, users, grants, payments, settings) — JWT-protected
  - `bot_service` — endpoints the bot itself calls (runtime-config, texts, users, trial, tariffs, payments) — bot service token only
- `app/services/`
  - `xray.py` — generates Xray JSON config, gRPC user add/remove, traffic stats, log tailing. File lock `/etc/xray/config.lock` serializes concurrent writes
  - `stats.py` — traffic collection, limit enforcement, monthly counter reset (clears `traffic_*` `NotificationLog` rows so warnings re-arm)
  - `panel_proxy.py` — Panel Federation HTTP client: `FederationClient` talks to linked panels, proxies user/inbound CRUD operations to remote panels based on `TariffItem.panel_id` routing
  - `sub_cache.py` — Redis-backed subscription response cache
  - `runtime_identity.py` — generates UUIDs / keys for protocols
  - `device_tracking.py` — HWID-aware device limit enforcement
  - `billing.py` — YooKassa SDK wrapper, `create_checkout`, `apply_payment` (atomic claim via `UPDATE … WHERE status='pending'` to prevent double-provision)
  - `provisioning.py` — single gateway for tariff grants: extends an existing `Client` for the same (telegram_id, inbound_tag) or creates one; resets counters; clears `traffic_*` `NotificationLog`; proxies to linked panels via `panel_proxy`
  - `bot_events.py` — `publish(event_type, telegram_id, payload)`: dual-write to `bot_event` table and Redis pubsub channel `bot:events`. Marks `delivered_at` on successful Redis publish.
- `app/jobs/`
  - `billing.py` — `auto_renew_free_users` (free-tier renewal, pause+notify on archive/disable)
  - `payments.py` — `poll_pending_payments` (30s webhook fallback), `cleanup_old_payments` (24h, cancels stuck pending + publishes notification)
  - `notifications.py` — `send_expiry_notifications`, `send_traffic_notifications`, `cleanup_bot_events`, `replay_undelivered_bot_events`

### Frontend (`frontend/src/`)
- `pages/` — `Dashboard` (inbound/outbound management), `Statistics` (traffic analytics), `Routing`, `Panels` (federation management), `Bot` (billing UI), `System` (settings + logs + backup + about), `Login`
- `components/bot/` — `TariffsTab`, `TariffDrawer`, `TariffsTable`, `TariffRowMenu`, `UsersTab`, `UserDrawer`, `GrantsTab`, `PaymentsTab`, `PaymentStatusBadge`, `TextsTab`, `SettingsTab`, `TrialCard`
- `components/inbound/` — `InboundForm`, `UserForm`
- `components/ui/` — shared primitives (`Select`, `Modal`, `ConfirmationModal`, `Button`, `Input`, `TagInput`, etc.)
- `lib/api.ts` — axios client with auth interceptor (auto-logout on 401)
- `lib/types.ts` — TS interfaces for every API entity
- `lib/protocols.ts` — protocol + stream-settings definitions
- `stores/` — Zustand stores for auth + log state

### Telegram Bot (`tg_bot/`)
- `main.py` — aiogram entry: bootstraps `runtime_config` → builds `Bot` → starts polling + bot-events consumer; on runtime change (token/proxy hot-swap) it stops polling, closes the old aiohttp session, builds a new `Bot`, and restarts polling **without** restarting the consumer (consumer holds a Bot-accessor closure, not a fixed ref)
- `runtime_config.py` — polls `GET /api/bot/runtime-config` every 60s; emits a change event when bot_token / telegram_proxy_url shift
- `backend_client.py` — thin async HTTP wrapper around `/bot-service/*` endpoints
- `api_service.py` — multi-panel manager (`MultiPanelManager`); connects to the master panel via `BACKEND_API_URL`, routes user CRUD and subscription queries through the single master entry
- `bot_events_consumer.py` — subscribes to Redis `bot:events`, dispatches `payment_*` / `access_*` / `expiry_notification` / `traffic_notification` / `texts_changed` / `user_*` events
- `i18n.py` — `BotText` cache, `t(key, lang, **kwargs)` formatter
- `middleware.py` — `LangMiddleware`: per-user language lookup, cache, invalidation on `user_language_changed`
- `handlers/admin.py`, `handlers/user.py`, `handlers/catalog.py` — message + callback handlers
- `keyboards.py`, `states.py`, `utils.py` — UI builders, FSM states, helpers
- `config.py` — env validation: `BACKEND_API_URL`, `BOT_SERVICE_TOKEN`, `BOT_LOG_LEVEL`

The bot is **backend-client** (not standalone) — it has no local SQLite. All state (users, languages, notifications, payments) lives in the panel's `panel.db`.

## Key Concepts

### Xray integration
`xray.py` both writes the full JSON config to `/etc/xray/config.json` and manages live users via the Xray Handler/Stats gRPC API. Config regeneration and Xray restart happen together when inbounds/outbounds change. The file lock `/etc/xray/config.lock` serializes concurrent writers (request handlers + the scheduler). gRPC requires gevent-compatible setup: `grpc_gevent.init_gevent()` runs at app startup before any gRPC import; current pin `grpcio==1.66.2` on Python 3.12.

### Traffic enforcement
`stats.py` polls per-user up/down via Xray gRPC every 10s, writes to `Client.up`/`down` and upserts hourly `TrafficSnapshot` rows. `check_limits` (60s) removes users that exceed limit or expiry. Monthly resets (per-client `reset_day`) zero the counters **and** delete that client's `traffic_*` `NotificationLog` rows so the next cycle's warnings can fire.

### Background scheduler jobs

| Job | Interval | What it does |
|---|---|---|
| `sync_traffic` | 10s | Per-user up/down from Xray gRPC; upserts `TrafficSnapshot` via raw SQL `ON CONFLICT DO UPDATE` |
| `check_limits` | 60s | Removes expired/over-limit users |
| `parse_logs` | 15s | Tails Xray access logs into `DomainStat` (skips bare IPs) |
| `cleanup_stats` | 24h | Deletes `DomainStat` rows > 90d |
| `poll_linked_panels` | 10s | Pings each enabled `LinkedPanel`, updates `status`/`last_poll`/`last_error` |
| `auto_renew_free_users` | 15m | Re-provisions due `billing='free'` grants; pauses + emits `access_paused` on tariff archive/disable |
| `poll_pending_payments` | 30s | Webhook fallback; reconciles pending YooKassa payments older than 30s, younger than 24h |
| `cleanup_old_payments` | 24h | Cancels `pending > 24h` (and publishes `payment_cancelled` so users find out); deletes terminal records `> 90d` |
| `send_expiry_notifications` | 15m | 3d/1d/1h/expired warnings (dedup via `notification_log`, renew button shown only when tariff is still purchasable) |
| `send_traffic_notifications` | 15m | 80%/95%/exhausted warnings (dedup + per-cycle re-arm) |
| `replay_undelivered_bot_events` | 60s | Re-publishes any `bot_event` row with `delivered_at IS NULL` and `created_at < now - 30s` |
| `cleanup_bot_events` | 24h | Prunes delivered events > 7d, undelivered > 30d |

### Backend error handling pattern
All API handlers follow a two-catch pattern. `ValueError` is the type for user-facing validation errors — propagated as HTTP 400 with the message shown to the user. Bare `Exception` means an unexpected server fault and returns HTTP 500 with a generic message. Always raise `ValueError` (not `Exception`) for input validation failures so the error reaches the user.

### Auth
Five decorators in `app/utils.py`:
- `token_required` — admin JWT only. Used on `/api/backup`, `/api/restore`, all `bot_admin` endpoints.
- `bot_service_token_required` — fixed token from `SystemSetting('bot_service_token')`, compared in constant time. Used on all `bot_service.py` endpoints + `/billing/checkout`.
- `admin_or_bot_token_required` — accepts either. Used on `/api/panels`, parts of `/api/system` (e.g. `/api/restart`, `/api/stats/system`) — needed because the bot legitimately needs to create/update/delete users.
- `federation_token_required` — validates the `federation_token` from a linked panel's `FederationConfig`. Used on federation endpoints that remote panels call.
- `admin_or_federation_token_required` — accepts admin JWT or federation token. Used on `/api/inbound` user/inbound CRUD endpoints so linked panels can proxy operations.

JWT tokens (2h expiry) carry a `pwdv` (password version) field tied to `Admin.password_changed_at` — changing the admin password instantly invalidates all existing tokens. The axios interceptor in `lib/api.ts` auto-logs out on any 401.

### Bot billing flow

1. Bot → `POST /api/billing/checkout` with `{telegram_id, tariff_id, lang}` (bot service token)
2. `services/billing.create_checkout` creates a `Payment` row (status='pending', placeholder yookassa_id), calls `yookassa.Payment.create` with a `gevent.with_timeout(8s)` + 1 retry on the same idempotence key, then persists `yookassa_id` + `confirmation_url`
3. Bot opens the YooKassa URL in the user's Telegram chat
4. User pays → YooKassa POSTs `/api/billing/yookassa/webhook` from one of their whitelisted IPs (rightmost XFF entry, set by Caddy)
5. Webhook → `services/billing.apply_payment(payment)`:
   - Idempotency fast-path: `if payment.status == 'succeeded': return`
   - **Atomic claim**: `UPDATE payment SET status='processing' WHERE id=:id AND status='pending'`; if rowcount=0, the poll cron already grabbed it — return
   - Re-validate tariff (still purchasable, items not removed, private+no-grant → fail)
   - `provisioning.apply_tariff_for_user` → extends or creates a `Client` per `TariffItem`
   - Sets `status='succeeded'`, publishes `payment_succeeded` to `bot:events`
   - On provisioning exception, releases claim back to `pending` so the poll cron retries

`poll_pending_payments` (30s) is the fallback when the webhook never arrived; it targets payments aged 30s–24h and runs the same `apply_payment`.

### Provisioning (`services/provisioning.py`)

`apply_tariff_for_user(telegram_id, tariff, source)` is the **single gateway** for every grant path (admin grant, trial, paid webhook, free auto-renew). For each `TariffItem`:
- If a `Client` already exists for the same (telegram_id, inbound_tag): extend it — bump `expiry_time`, reset `up/down/last_reset_time`, refresh `limit_bytes`, set `enable=True`, clear `traffic_*` `NotificationLog` rows (so the new cycle's warnings can fire)
- Otherwise create a new `Client` with a unique email (`tg<id>_<inbound_tag>` or `_<hex6>` on collision)

Single `_sync_after_provision` call after the loop: regenerates Xray config, restarts container if needed, proxies the change to linked panels via `panel_proxy`, invalidates the Redis sub-cache.

### Panel Federation

The panel supports a *federation* model where a master panel manages remote *linked panels*. `LinkedPanel` rows store URL and a `federation_token` for mutual authentication. `FederationConfig` is a singleton row on the child side that stores the master link credentials.

The master proxies user/inbound CRUD to linked panels via `services/panel_proxy.py` (`FederationClient`). `TariffItem.panel_id` optionally routes a tariff item to a specific linked panel — when set, provisioning creates the user on that remote panel instead of locally.

The `poll_linked_panels` job (10s) pings each enabled `LinkedPanel` and updates `status`/`last_poll`/`last_error`. Subscription links (`api/subscription.py`) can merge entries from linked panels visible to the requesting client, cached in Redis.

Inbound CRUD endpoints (`api/inbound.py`) accept both admin JWT and federation tokens via `admin_or_federation_token_required`, so linked panels can proxy operations back through the master.

### Bot event recovery buffer

`services/bot_events.publish` writes a `BotEvent` row to SQLite *first*, then attempts `redis.publish('bot:events', …)`. On successful publish it sets `delivered_at = now`. The `replay_undelivered_bot_events` cron (60s) re-publishes any row older than 30 seconds with `delivered_at IS NULL`. Caveat: Redis `PUBLISH` succeeding with `subscriber_count=0` (e.g. bot is down) still marks `delivered_at` because we don't check the return code — this means the recovery buffer protects against Redis outages but not consumer outages. The current behavior is intentional (a temporary bot stop is the supported way to suppress a wave of grant notifications during bulk operations) but is worth keeping in mind.

### Telegram user lifecycle

- `TelegramUser` row is upserted on each `/start` via `POST /bot-service/users` (created with `language='ru'`, `language_chosen=False`, `blocked=False` by default)
- User chooses RU/EN on first start → `language_chosen=True`
- Admin can `block` a user (`POST /bot/users/<id>/block`): cancels all `UserTariffAccess` grants, disables all `Client` rows, **removes them from Xray runtime via gRPC for vless/vmess (otherwise triggers config regen + restart)**, and propagates the deletion to linked panels via `panel_proxy`. `unblock` only clears the flag — does **not** restore cancelled tariffs or re-enable clients.
- `client.telegram_id` is the link between Telegram users and Xray accounts; admin grants find the matching client by `(telegram_id, inbound_tag)` and extend in place, preserving UUIDs.

### Stream settings storage
Inbound stream settings are stored as a single JSON blob in `Inbound.stream_settings`. This blob carries extra UI-only keys beyond what Xray understands (`ssMethod`, `ssPassword`, `ssNetwork`, `authUser`, `authPass`, `wgSecretKey`, `wgPublicKey`, `wgMTU`). `generate_config_file()` strips these keys before writing the Xray config. When adding a new protocol, follow this pattern: store all metadata in the blob, strip extra keys in the stripping list at the bottom of `generate_config_file()`.

### Protocol/stream types
Protocol details live in `frontend/lib/protocols.ts` (UI-facing) and are serialized to JSON in backend models. Client IDs must be valid UUIDs for VLESS/VMess/Trojan, valid WireGuard private keys for WireGuard. Shadowsocks 2022 server/user passwords must be base64-encoded keys of the correct byte length (16 bytes for AES-128, 32 bytes for AES-256 and ChaCha20).

### Subscription links
`api/subscription.py` serves `GET /api/sub/<uuid_str>` — UUID-keyed, so renaming `Client.email` does NOT break a user's existing app config. The response can merge entries from linked panels visible to the user. Cached in Redis with a configurable TTL (`subscription_update_interval_hours` SystemSetting).

### Custom Select component
`components/ui/Select.tsx` renders a portal-based dropdown instead of a native `<select>`. It synthesizes a `React.ChangeEvent<HTMLSelectElement>` in its `onChange`. When used with react-hook-form, always spread `{...register('fieldName')}` so the `name` prop is passed — react-hook-form looks up the field by `event.target.name` and silently ignores the change if `name` is missing or empty.

### Default outbounds
On startup, `direct` (freedom) and `block` (blackhole) outbounds are auto-created if missing. These are always re-enabled if disabled — do not delete them.

### Database migrations
`db_migration.py` is a custom migration system (not Flask-Migrate). Current schema version is **`15`**, tracked via `PRAGMA user_version`. The script is idempotent — runs on every backend startup, uses `CREATE TABLE IF NOT EXISTS` for new tables and `ALTER TABLE ADD COLUMN` (with `_add_column_if_missing` guard) for column additions. All `ALTER`s are SQLite metadata-only (O(1)), so migration time is independent of row count.

Bot texts have their own version: `CURRENT_BOT_TEXTS_VERSION = 15`. Bumping it triggers a one-shot force-reseed at next startup — every `(key, lang)` pair from `app/data/bot_texts_defaults.yaml` is upserted. **This overwrites admin-edited texts** if their key is in the YAML.

When adding a new table: add a `_ensure_<name>_table` function, call it from `migrate_sqlite_db`, bump `CURRENT_DB_VERSION`.

### Statistics storage
`TrafficSnapshot` stores hourly traffic deltas per entity (user or inbound) **forever** — space is ~100 bytes × entities × 8760 hours/year, negligible for typical deployments. `DomainStat` stores daily domain hit counts and is pruned to 90 days. Both use SQLite `ON CONFLICT DO UPDATE` upserts via `literal_column()` + raw `text()` SQL — do not replace with ORM insert, it breaks atomicity.

### Secret path injection
The frontend is served under `PANEL_SECRET_PATH`. At container startup, `frontend/entrypoint.sh` injects `window.__PANEL_BASE_URL__` into `index.html` and generates `nginx.conf` from `nginx.conf.template`. All traffic outside the secret path returns 404.

### gevent + gRPC
`grpc_gevent.init_gevent()` is called at app startup before any gRPC usage. The backend runs under gunicorn+gevent (single worker), so gRPC calls must be gevent-compatible. Current pin: `grpcio==1.66.2` on Python 3.12.

### ProxyFix
Configured in `app/__init__.py` as `ProxyFix(app.wsgi_app, x_for=2, …)`. **Heads-up:** with Caddy as the only reverse proxy this should arguably be `x_for=1`; the higher value means `request.remote_addr` can be influenced by an attacker-supplied left-most XFF entry through Caddy. The YooKassa webhook works around this by *not* trusting `remote_addr` — it parses the rightmost XFF entry directly. If you touch other webhook-style endpoints, do the same.

### Frontend tab/slider style
All horizontal tab bars use a consistent pill style: container `bg-white/[0.04] p-1 rounded-2xl border border-white/[0.05]`, active item is an absolutely-positioned `motion.div` with `layoutId` and `bg-gradient-to-br from-primary/25 to-violet-600/20 rounded-xl border border-white/[0.1] shadow-[0_0_12px_rgba(208,188,255,0.12)]`, spring transition `stiffness: 500, damping: 35`. Do not use plain CSS active classes for tab bars.

## CI Checks (run on every push)

All checks must pass before code reaches `main`. Run locally before pushing:

| Check | Command |
|---|---|
| Python lint + format | `ruff check backend/ tg_bot/` · `ruff format --check backend/ tg_bot/` |
| TypeScript typecheck | `cd frontend && npx tsc --noEmit` |
| ESLint | `cd frontend && npm run lint` |
| Prettier | `cd frontend && npm run format:check` |
| Frontend build | `cd frontend && npm run build` |
| Dockerfile lint | hadolint (runs in CI only) |

`ruff format <dir>` and `npm run format` auto-fix formatting issues — run them before committing, not after CI fails.

CI does **not** run pytest. Backend tests are still useful locally and should be added when behavior changes — see `backend/tests/` for patterns.

## Git Workflow

### Feature branches — always
All work on service code (`backend/`, `frontend/`, `tg_bot/`, `caddy/`) goes in a feature branch, never directly on `main`.

```bash
git checkout -b feat/my-feature
# work, commit freely — history doesn't matter here
git checkout main
git merge --squash feat/my-feature
git commit -m "feat(service): concise description"
git push
git branch -D feat/my-feature   # -D because squash means the branch is "unmerged" by git's count
```

`--squash` collapses all branch commits into one staged diff. Write one clean commit message, push once — CI runs once, one commit appears in `main`.

**Committing directly to `main` is only acceptable for CI/config-only changes** (`.github/`, `scripts/`, `CLAUDE.md`, `docker-compose*.yml`) that don't touch service source files and therefore don't trigger a release.

### CI/CD skip tags
| Tag | Effect |
|---|---|
| `[skip ci]` | GitHub skips **all** workflows |
| `[skip release]` | Release job is skipped even if `versions.json` was bumped — use when restoring `versions.json` or intentionally editing it without rebuilding |

### How the release pipeline works
Release is **driven entirely by `versions.json`**. You decide what to ship by editing the file yourself — nothing auto-bumps.

1. Bump the service(s) you want to release in `versions.json` (e.g. `"bot": "2.0.0"` → `"2.0.1"`).
2. Update the matching line in `.env.example` so deployers pin the new tag. `scripts/bump_version.py patch bot` does both at once; alternatively edit by hand.
3. Merge to `main`. The release workflow triggers only when `versions.json` changes on `main`.
4. CI diffs the new `versions.json` against the previous commit and builds/pushes **only the services whose version string changed**. If only `xray_core_ref` changed it's a no-op; bump `backend` too to force a rebuild.
5. CI does **not** commit anything back to `main`. There is no auto-bump commit.

Force-pushing rewrites history — CI can't diff against the old SHA and falls back to `HEAD~1..HEAD`. Avoid force-pushing `main`; use feature branches.

### Panel Federation deploy ordering
When the schema bumps (any `CURRENT_DB_VERSION` change), **deploy master and all linked panels in the same wave**. A master on a newer schema may push user/tariff structures that an older linked panel can't parse. Backup first (`GET /api/backup`), then `docker compose pull && up -d` everywhere.

## Configuration

Copy `.env.example` to `.env`. Key variables:
- `PANEL_DOMAIN`, `PROXY_DOMAIN`, `PANEL_SECRET_PATH` — routing/TLS
- `SECRET_KEY`, `PANEL_ADMIN_USER`, `PANEL_ADMIN_PASSWORD`
- `XRAY_CORE_REF` — Xray-core version to compile into the Docker image (build-time only)
- `RATELIMIT_STORAGE_URI` — Redis URI for rate limiting

Bot configuration is **not** in `.env`. It lives in `SystemSetting` rows managed via **Bot → Settings** in the panel UI: `bot_token`, `admin_telegram_ids`, `bot_service_token`, YooKassa `shop_id` / `secret_key`, `display_timezone`. The bot container only needs two env vars: `BACKEND_API_URL` and `BOT_SERVICE_TOKEN`. Changes take effect within ~60s without restarting the bot.

**Local vs. production validation:** When `PANEL_DOMAIN` is a local hostname (`localhost`, `*.local`, or an IP literal), the app relaxes requirements: weak `SECRET_KEY` is allowed, default `admin:admin` credentials are allowed, `memory://` rate limiting is allowed. For any real domain, all three are enforced on startup and the app refuses to start if they fail.
