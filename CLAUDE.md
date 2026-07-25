# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ITG Xray Panel is a full-stack VPN/proxy management panel for the [Xray-core](https://github.com/XTLS/Xray-core) proxy platform. It manages inbound/outbound proxy configurations, user accounts with traffic limits, routing rules, real-time traffic statistics, and a **YooKassa-backed billing system** with a fully customisable Telegram bot. A master panel can **federate** any number of remote panels.

**Stack:** Python 3.12 · Flask · gunicorn+gevent · SQLAlchemy · SQLite · Xray-core via gRPC · React + TypeScript + Vite · Aiogram 3 · Redis · Caddy (caddy-l4 SNI routing) · Docker Compose

## Commands

### Docker (primary workflow)
```bash
docker compose up                              # Start all services (dev)
docker compose -f docker-compose.prod.yml up   # Production

# Rebuild and restart a single service after code changes:
docker compose build frontend && docker compose up -d frontend
docker compose build backend  && docker compose up -d backend
docker compose build bot      && docker compose up -d bot
docker compose build caddy    && docker compose up -d caddy
```

### Backend (Python/Flask)
```bash
cd backend
uv sync                        # install deps into .venv (+ dev group)
uv run python run.py           # Dev server on :5000
uv run python migrate_db.py    # Run DB migrations standalone

uvx ruff check backend/
uvx ruff format backend/           # auto-fix formatting
uvx ruff format --check backend/   # CI mode — no changes, exit 1 if dirty

uv run pytest tests/                  # 850+ unit + integration tests
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
uv sync
BACKEND_API_URL=http://backend:5000/api BOT_SERVICE_TOKEN=<token> uv run python main.py

uvx ruff check tg_bot/
uvx ruff format tg_bot/
uvx ruff format --check tg_bot/
```

### Caddy / caddygen (Go)
```bash
cd caddy/caddygen && go test ./...   # tests for the routes.yaml → Caddy-JSON generator
```

### Certificates & demo data
```bash
bash scripts/generate_certs.sh        # issue/renew the LE SAN cert (stops caddy, certbot --standalone, installs into ./certs, restarts caddy)
bash scripts/generate_local_cert.sh   # self-signed cert for local domains
```
`scripts/seed_demo.py` + `scripts/seed_bot_demo.py` populate realistic demo inbounds/users/tariffs/payments/traffic (run them where the app is importable — e.g. copied into the backend container; idempotent, tagged `[demo]`). Handy for screenshots and manual testing.

## Architecture

### Docker Services
| Service | Role |
|---|---|
| `xray` | Xray-core proxy engine |
| `backend` | Flask API + APScheduler crons (gunicorn + gevent, single worker) |
| `frontend` | React app served by Nginx |
| `caddy` | Reverse proxy — caddygen-built native JSON, SNI routing on `:443` (caddy-l4), `:80→:443` redirect, TLS from mounted certs, decoy masquerade |
| `redis` | Rate limiting + sub-cache + bot pubsub channel |
| `socket-proxy` | Restricts Docker socket access to specific API ops |
| `bot` | Telegram bot (Aiogram, asyncio) — runs on the master only |

Three networks: `panel-net` (frontend/backend/caddy + xray + bot — the only one with internet egress) plus two `internal: true` segments: `redis-net` (backend ↔ redis ↔ bot) and `dockersock-net` (backend ↔ socket-proxy). The split (formerly a single `control-net`) keeps the Docker-socket proxy reachable only by `backend` and denies internet to both `socket-proxy` and `redis`. Key volumes: `shared_config:/etc/xray`, `xray_logs:/var/log/xray`, `./db_data:/app/db`, `./certs:/root/cert:ro`. Published ports on `caddy`: `80:80`, `443:443` (TCP only — there is no `443/udp` / HTTP-3).

In the split Postgres deployment, `PANEL_ROLE` selects one of four Flask app factories (`panel_core.roles.{master,worker,sub,botapi}`): `master` (default — billing/admin API, no local Xray) runs against Postgres via `DATABASE_URL`; `worker` — called a **node** below — has its own Xray, but (per `docker-compose.node.yml` / `.env.example`) has no `DATABASE_URL` at all, so it runs against its own local SQLite (`./db_data`) as a cache/fallback rather than sharing the master's Postgres; `sub` serves subscription links only; `bot` serves `/bot-service/*` for the Telegram bot (bot-api). A node and a Panel Federation `LinkedPanel` (see Panel Federation below) are two views of the same thing, not separate systems: the node is the process role (`PANEL_ROLE=worker`), while `LinkedPanel` is the row the master's Postgres uses to address it (url + `federation_token`). The master routes provisioning to a node through exactly that federation path — `TariffItem.panel_id` → `LinkedPanel` → `FederationClient.provision()` → `POST /api/federation/provision` on the node (`services/panel_proxy.py`, `api/federation.py`) — which is also *why* a node can't resolve `lang`/`renewable` itself: it has no Postgres access to `TelegramUser`/`Tariff`, only its own local SQLite.

Two Redis instances play different roles once nodes are split out. The `redis` above is per-node private state (rate limiting + sub-cache, `RATELIMIT_STORAGE_URI`) and never leaves that node. The `bot:events` bus is separate: a data-tier Redis defined in `docker-compose.postgres.yml`, shared by master/bot-api/sub/bot and every node, addressed by `BOT_EVENTS_REDIS_URI` (defaults to `RATELIMIT_STORAGE_URI`, so anything sharing Redis with the master needs no extra config — only a node, whose `RATELIMIT_STORAGE_URI` points at its own local Redis, must set it explicitly). That data-tier Redis ACLs two users: `node` (publish-only into `bot:events`, nothing else) and `panel` (full access). See Bot event recovery buffer and Configuration below.

### Backend (`backend/`)
- `app/__init__.py` — Flask app factory; registers blueprints, extensions, ProxyFix, APScheduler jobs
- `app/models.py` — SQLAlchemy models (22 total). Core: `Admin`, `Inbound`, `Client`, `Outbound`, `RoutingProfile`, `Balancer`, `SystemSetting`, `TrafficSnapshot`, `NodeTrafficSnapshot`, `DomainStat`, `LinkedPanel`, `FederationConfig`, `ClientDevice`. Billing/bot: `Tariff`, `TariffItem`, `UserTariffAccess`, `Payment`, `BotText`, `BotEvent`, `TelegramUser`, `NotificationLog`, `NotificationClaim`. **FK enforcement is OFF** — `extensions.py` sets WAL/synchronous/busy_timeout/temp_store but **not** `PRAGMA foreign_keys=ON`, so FK constraints are advisory (deleting a parent leaves dangling child refs rather than cascading/erroring; e.g. `delete_tariff_permanent` can orphan `Client.tariff_id`). Exception: deleting a `LinkedPanel` (`delete_panel`) or an `Inbound` (`delete_inbound`, local + remote-via-`panel_id`) app-level cascades the matching `TariffItem` rows through `services/tariffs.purge_tariff_items`, which also disables any tariff left with zero items — so a removed panel/inbound can no longer orphan a `TariffItem` and 500 provisioning.
- `app/extensions.py` — Shared Flask extensions (db, migrate, APScheduler, Flask-Limiter, SQLite PRAGMAs)
- `app/utils.py` — JWT helpers + auth decorators: `token_required` (admin JWT only), `bot_service_token_required` (bot service token only), `admin_or_bot_token_required` (accepts either), `federation_token_required` (validates federation token from linked panels), `admin_or_federation_token_required` (accepts admin JWT or federation token). The latter two support the Panel Federation system. `admin_or_bot_token_required` is used on `/api/inbound`, `/api/panels`, and most `/api/system` endpoints — **but NOT on `/api/backup` and `/api/restore`** which take admin-only after the ultrareview hardening.
- `app/api/`
  - `auth` — login / logout
  - `inbound`, `outbound`, `routing`, `panels`, `federation`, `subscription`, `statistics`, `system` — core panel
  - `billing` — YooKassa checkout + webhook. The webhook is **unsigned**, so the body is treated only as a trigger: the handler re-fetches the authoritative status from YooKassa (`fetch_remote_status`) before provisioning, so a forged notification does nothing
  - `bot_admin` — admin UI endpoints (tariffs, texts, users, grants, payments, settings) — JWT-protected
  - `bot_service` — endpoints the bot itself calls (runtime-config, texts, users, trial, tariffs, payments) — bot service token only
- `app/services/`
  - `xray.py` — generates Xray JSON config, gRPC user add/remove, traffic stats, log tailing. File lock `/etc/xray/config.lock` serializes concurrent writes
  - `stats.py` — traffic collection, limit enforcement, monthly counter reset (clears `traffic_*` `NotificationLog` rows so warnings re-arm); `check_limits_and_reset` and `sync_traffic_stats` also emit `expiry_notification`/`traffic_notification` events inline (see `notifications.py` below and Bot event recovery buffer)
  - `panel_proxy.py` — Panel Federation HTTP client: `FederationClient` talks to linked panels, proxies user/inbound CRUD operations to remote panels based on `TariffItem.panel_id` routing. `get_panel_snapshot` (cached, 60s TTL) vs `fetch_panel_snapshot_live` (live, no cache)
  - `sub_cache.py` — Redis-backed subscription response cache
  - `runtime_identity.py` — generates UUIDs / keys for protocols
  - `device_tracking.py` — HWID-aware device limit enforcement
  - `billing.py` — YooKassa SDK wrapper, `create_checkout`, `apply_payment` (atomic claim via `UPDATE … WHERE status='pending'` to prevent double-provision)
  - `provisioning.py` — single gateway for tariff grants: extends an existing `Client` for the same (telegram_id, inbound_tag) or creates one; resets counters; clears `traffic_*` `NotificationLog`; proxies to linked panels via `panel_proxy`
  - `bot_events.py` — `publish(event_type, telegram_id, payload)`: dual-write to `bot_event` table and Redis pubsub channel `bot:events`. Marks `delivered_at` on successful Redis publish.
  - `notifications.py` — `evaluate_expiry`/`evaluate_traffic` classify a client into a warning bucket (3d/1d/1h/expired; 80%/95%/exhausted); `emit_if_new` dedups via `NotificationLog` and publishes the bare fact only (no `lang`/`renewable` — a node can't resolve those) with `node` (the node's own `PANEL_DOMAIN`) and `inbound_tag` in the payload; `claim_notification` is the Postgres-backed atomic cross-node claim behind `NotificationClaim` (see Bot event recovery buffer)
  - `version_check.py` — `fetch_latest` (6h cron): pulls the published `versions.json` from GitHub and caches it in a `SystemSetting`, powering the "update available" indicator on the System → About tab
  - `bot_status.py` — small cache for the bot's reported version/health surfaced in the UI
- `app/jobs/`
  - `billing.py` — `auto_renew_free_users` (free-tier renewal, pause+notify on archive/disable)
  - `payments.py` — `poll_pending_payments` (30s webhook fallback), `reconcile_refunds` (1h refund-webhook fallback → `billing.handle_refund` revokes access), `cleanup_old_payments` (24h, cancels stuck pending + publishes notification)
  - `notifications.py` — `cleanup_bot_events`, `replay_undelivered_bot_events` (also registered on the worker role, not just master). There is no `send_expiry_notifications`/`send_traffic_notifications` cron — expiry and traffic warnings are emitted inline from `stats.py`'s `check_limits_and_reset` and `sync_traffic_stats` via `services/notifications.emit_if_new`
  - `panels.py` — `poll_linked_panels` (10s linked-panel health poll)

### Frontend (`frontend/src/`)
- `pages/` — `Dashboard` (inbound/outbound management), `Statistics` (traffic analytics), `Routing`, `Panels` (federation management), `Bot` (billing UI), `System` (settings + logs + backup + about), `Login`
- `components/bot/` — `TariffsTab`, `TariffDrawer`, `TariffsTable`, `TariffRowMenu`, `UsersTab`, `UserDrawer`, `GrantsTab`, `PaymentsTab`, `PaymentStatusBadge`, `TextsTab`, `SettingsTab`, `TrialCard`
- `components/inbound/` — `InboundForm`, `UserForm`
- `components/ui/` — shared primitives (`Select`, `Modal`, `ConfirmationModal`, `Button`, `Input`, `TagInput`, etc.)
- `lib/api.ts` — axios client with auth interceptor (auto-logout on 401)
- `lib/types.ts` — TS interfaces for every API entity
- `lib/protocols.ts` — protocol + stream-settings definitions
- `stores/` — Zustand stores for auth + log state

### Caddy (`caddy/`)
- `routes.yaml` — declarative per-SNI routes (the only hand-edited Caddy config). Fields: `match` (SNI host, `${ENV}` interpolated), `upstream` (`host:port`), `tls` (terminate vs raw passthrough), `only_paths` (path-prefix allowlist → 404, implies `tls`). A route whose `match` is empty after interpolation is **dropped** (so an empty `SUB_DOMAIN` drops the subscription route).
- `caddygen/` — small Go program that reads `routes.yaml` + env and emits Caddy's **native JSON** (entrypoint runs `caddygen → caddy validate → caddy run`). See "TLS, Caddy & certificates" below.

### Telegram Bot (`tg_bot/`)
- `main.py` — aiogram entry: bootstraps `runtime_config` → builds `Bot` → starts polling + bot-events consumer; on runtime change (token/proxy hot-swap) it stops polling, closes the old aiohttp session, builds a new `Bot`, and restarts polling **without** restarting the consumer (consumer holds a Bot-accessor closure, not a fixed ref)
- `runtime_config.py` — polls `GET /api/bot/runtime-config` every 60s; emits a change event when bot_token / telegram_proxy_url shift
- `backend_client.py` — thin async HTTP wrapper around `/bot-service/*` endpoints
- `api_service.py` — multi-panel manager (`MultiPanelManager`); connects to the master panel via `BACKEND_API_URL`, routes user CRUD and subscription queries through the single master entry
- `bot_events_consumer.py` — subscribes to Redis `bot:events`, dispatches `payment_*` / `access_*` / `expiry_notification` / `traffic_notification` / `texts_changed` / `user_*` events
- `i18n.py` — `BotText` cache, `t(key, lang, **kwargs)` formatter (missing key → `⟨key⟩`, falling back to the other language first)
- `middleware.py` — `LangMiddleware`: per-user language lookup, cache, invalidation on `user_language_changed`
- `handlers/admin.py`, `handlers/user.py`, `handlers/catalog.py` — message + callback handlers
- `keyboards.py`, `states.py`, `utils.py` — UI builders, FSM states, helpers
- `config.py` — env validation: `BACKEND_API_URL`, `BOT_SERVICE_TOKEN`, `BOT_LOG_LEVEL`

The bot is **backend-client** (not standalone) — it has no local SQLite. All state (users, languages, notifications, payments) lives in the panel's `panel.db`. **One Telegram token may only long-poll once**, so run the `bot` service against a single master; never start a second poller with the same token (it would 409 the first).

## Key Concepts

### Xray integration
`xray.py` both writes the full JSON config to `/etc/xray/config.json` and manages live users via the Xray Handler/Stats gRPC API. Config regeneration and Xray restart happen together when inbounds/outbounds change. The file lock `/etc/xray/config.lock` serializes concurrent writers (request handlers + the scheduler). gRPC requires gevent-compatible setup: `grpc_gevent.init_gevent()` runs at app startup before any gRPC import; current pin `grpcio==1.66.2` on Python 3.12.

### TLS, Caddy & certificates
Caddy does **not** use automatic ACME. `caddy/caddygen/` generates Caddy's native JSON from `caddy/routes.yaml` at container start (`caddy validate` runs before `caddy run`, so a bad config fails fast). The generated config uses the **caddy-l4** layer4 app listening on `:443`, routing by **TLS SNI**:
- `PROXY_DOMAIN` (decoy) → raw-TCP passthrough with PROXY-protocol to `xray:443`, so Xray sees the real TLS/REALITY handshake (masquerade).
- `PANEL_DOMAIN` / `SUB_DOMAIN` → TLS terminated at Caddy, PROXY-protocol'd to a per-route loopback HTTP server (security headers + CSP, optional path filter) → `frontend:80` / `backend:5000`.
- caddygen also emits a plain `:80` server that 308-redirects everything to https.

Caddy loads **one** cert pair from `/root/cert/{fullchain,key}.pem` (mounted from `./certs`) for **all** terminated SNIs — a multi-domain deploy therefore needs a single **SAN** cert covering panel + sub. Issue/renew with `scripts/generate_certs.sh`: it stops Caddy (to free `:80`, which Caddy otherwise holds via the published port), runs `certbot certonly --standalone --expand` for `PANEL_DOMAIN` (+ `SUB_DOMAIN`), copies `fullchain.pem`/`privkey.pem` into `./certs`, and brings Caddy back (trap, even on failure). **Renewal is the same command, run manually** — there is no cron, and certbot's own timer can't bind `:80` while Caddy runs (and wouldn't propagate into `./certs` anyway). `scripts/generate_local_cert.sh` writes a self-signed cert for local domains. Both installers (`scripts/install_{dev,prod}.sh`) run a cert step **before** bringing Caddy up — Caddy won't start without `./certs/fullchain.pem`.

### Traffic enforcement
`stats.py` polls per-user up/down via Xray gRPC every 10s, writes to `Client.up`/`down` and upserts hourly `TrafficSnapshot` rows. `check_limits` (60s) removes users that exceed limit or expiry. Monthly resets (per-client `reset_day`) zero the counters **and** delete that client's `traffic_*` `NotificationLog` rows so the next cycle's warnings can fire.

### Background scheduler jobs

| Job | Interval | What it does |
|---|---|---|
| `sync_traffic` | 10s | Per-user up/down from Xray gRPC; upserts `TrafficSnapshot` via raw SQL `ON CONFLICT DO UPDATE`; emits `traffic_notification` inline at 80%/95%/exhausted (dedup via `NotificationLog`) |
| `check_limits` | 60s | Removes expired/over-limit users; emits `expiry_notification` inline at 3d/1d/1h/expired (dedup via `NotificationLog`) |
| `parse_logs` | 15s | Tails Xray access logs into `DomainStat` (skips bare IPs) |
| `cleanup_stats` | 24h | Deletes `DomainStat` rows > 90d |
| `poll_linked_panels` | 10s | Pings each enabled `LinkedPanel`; fresh `status`/`last_poll` go to Redis every poll, the SQLite row is written **only on status/error change** (the panels API overlays the Redis values) |
| `auto_renew_free_users` | 15m | Re-provisions due `billing='free'` grants; pauses + emits `access_paused` on tariff archive/disable (does **not** force-disable clients — they lapse via their own `expiry_time`) |
| `poll_pending_payments` | 30s | Webhook fallback; reconciles pending YooKassa payments older than 30s, younger than 24h |
| `reconcile_refunds` | 1h | Refund-webhook fallback; re-checks the most recent succeeded payments (≤30d, capped 200) and revokes access on any YooKassa now reports refunded (via `billing.handle_refund`) |
| `cleanup_old_payments` | 24h | Cancels `pending > 24h` (and publishes `payment_cancelled` so users find out); deletes terminal records `> 90d` |
| `replay_undelivered_bot_events` | 60s | Runs on **master and worker** roles; re-publishes any `bot_event` row with `delivered_at IS NULL` and `created_at < now - 30s` |
| `check_latest_version` | 6h | Fetches the published `versions.json` from GitHub, caches it in a `SystemSetting` to drive the "update available" indicator on System → About |
| `cleanup_bot_events` | 24h | Runs on **master and worker** roles; prunes delivered `bot_event` rows > 7d, undelivered > 30d, and `NotificationClaim` rows > 90d |

### Backend error handling pattern
All API handlers follow a two-catch pattern. `ValueError` is the type for user-facing validation errors — propagated as HTTP 400 with the message shown to the user. Bare `Exception` means an unexpected server fault and returns HTTP 500 with a generic message. Always raise `ValueError` (not `Exception`) for input validation failures so the error reaches the user.

### Auth
Five decorators in `app/utils.py`:
- `token_required` — admin JWT only. Used on `/api/backup`, `/api/restore`, all `bot_admin` endpoints.
- `bot_service_token_required` — fixed token from `SystemSetting('bot_service_token')`, compared in constant time. Used on all `bot_service.py` endpoints + `/billing/checkout`.
- `admin_or_bot_token_required` — accepts either. Used on `/api/panels`, parts of `/api/system` (e.g. `/api/restart`, `/api/stats/system`) — needed because the bot legitimately needs to create/update/delete users.
- `federation_token_required` — validates the `federation_token` from a linked panel's `FederationConfig`. Used on federation endpoints that remote panels call.
- `admin_or_federation_token_required` — accepts admin JWT or federation token. Used on `/api/inbound` user/inbound CRUD **and the `/users/bulk-*` + `/users/reset-traffic` batch endpoints**, so linked panels can proxy operations (and the master can fan a batch out to children).

JWT tokens (2h expiry) carry a `pwdv` (password version) field tied to `Admin.password_changed_at` — changing the admin password instantly invalidates all existing tokens. The axios interceptor in `lib/api.ts` auto-logs out on any 401.

### Bot billing flow

1. Bot → `POST /api/billing/checkout` with `{telegram_id, tariff_id, lang}` (bot service token)
2. `services/billing.create_checkout` creates a `Payment` row (status='pending', placeholder yookassa_id), calls `yookassa.Payment.create` with a `gevent.with_timeout(8s)` + 1 retry on the same idempotence key, then persists `yookassa_id` + `confirmation_url`
3. Bot opens the YooKassa URL in the user's Telegram chat
4. User pays → YooKassa POSTs `/api/billing/yookassa/webhook`. The webhook is **unsigned**, so the body is only a trigger — the handler re-fetches the authoritative status via `billing.fetch_remote_status(payment)`; a forged notification re-validates to nothing. (There is no IP whitelist — re-validation replaced it.)
5. On a confirmed `succeeded` status → `services/billing.apply_payment(payment)`:
   - Idempotency fast-path: `if payment.status == 'succeeded': return`
   - **Atomic claim**: `UPDATE payment SET status='processing' WHERE id=:id AND status='pending'`; if rowcount=0, the poll cron already grabbed it — return
   - Re-validate tariff (still purchasable, items not removed, private+no-grant → fail)
   - `provisioning.apply_tariff_for_user` → extends or creates a `Client` per `TariffItem`
   - Sets `status='succeeded'`, publishes `payment_succeeded` to `bot:events`
   - On provisioning exception, releases claim back to `pending` so the poll cron retries

`poll_pending_payments` (30s) is the fallback when the webhook never arrived; it targets payments aged 30s–24h and runs the same `apply_payment`.

### Provisioning (`services/provisioning.py`)

`apply_tariff_for_user(telegram_id, tariff, source)` is the **single gateway** for every grant path (admin grant, trial, paid webhook, free auto-renew). For each `TariffItem`:
- If `item.panel_id` is set → `proxy_provision` to that linked panel (the user is created/extended remotely, not locally).
- Else if a `Client` already exists for the same (telegram_id, inbound_tag): extend it — bump `expiry_time`, reset `up/down/last_reset_time`, refresh `limit_bytes`, set `enable=True`, clear `traffic_*` `NotificationLog` rows (so the new cycle's warnings can fire).
- Otherwise create a new `Client` with a unique email (`tg<id>_<inbound_tag>` or `_<hex6>` on collision).

Every call also clears that user's `NotificationClaim` rows for the tariff (`clear_notification_claims`), so the next expiry/traffic cycle can warn again after a renewal instead of staying suppressed by a stale cross-node claim.

Single `_sync_after_provision` call after the loop: regenerates Xray config (or gRPC-patches for vless/vmess fast-path), restarts container if needed, and invalidates the Redis sub-cache. `backfill_tariff` idempotently ensures every active holder has a key on every tariff inbound (local + remote) without touching existing keys.

### Panel Federation

A master panel manages remote *linked panels*. `LinkedPanel` rows store URL + a `federation_token`; `FederationConfig` is a singleton on the child storing the master's credentials. The master proxies user/inbound CRUD to linked panels via `services/panel_proxy.py` (`FederationClient`). `TariffItem.panel_id` optionally routes a tariff item to a specific linked panel — provisioning then creates the user there instead of locally. `poll_linked_panels` (10s) health-polls each panel. Subscription links (`api/subscription.py`) can merge entries from linked panels visible to the requesting client (Redis-cached). Inbound CRUD endpoints accept admin JWT **and** federation tokens (`admin_or_federation_token_required`) so children can proxy operations back through the master.

**Destructive user ops read a LIVE snapshot.** `block_user` / `unblock_user` / `revoke_tariff_from_user` in `bot_admin.py` enumerate the user's remote clients via `_remote_clients_by_telegram_id_live()` (which calls `fetch_panel_snapshot_live` per enabled panel), **not** the cached `get_panel_snapshot` — a stale/missing cache must never let a remote disable silently no-op. Panels that can't be reached are surfaced in the response's `panel_failures` (not skipped). `revoke_tariff_from_user` matches remote clients by the tariff's `(panel_id, inbound_tag)` items **and** `tariff_id` (mirroring the local match by `tariff_id`), so two tariffs sharing a remote inbound don't cross-disable. The read-only users UI still uses the cached `_remote_clients_by_telegram_id()`.

### Bulk user operations (cross-panel)

The Dashboard selection toolbar drives a set of batch endpoints in `api/inbound.py`, all `@admin_or_federation_token_required`: `POST /users/bulk-delete`, `/users/bulk-enable`, `/users/bulk-adjust-days`, `/users/bulk-adjust-traffic`, `/users/reset-traffic`, `/users/bulk-set-flow`.

Each request carries `users: [{tag, email, panel_id?}]`. `_split_users_by_panel` splits the batch into a local group and per-panel remote groups. Remote groups are forwarded to the owning linked panel's **identical** endpoint via `panel_proxy` (with `panel_id` stripped, so the child runs them purely locally — no recursion). Proxying is **best-effort**: an offline/erroring child is collected into an `errors[]` field in the response instead of failing the whole batch, and counts (`deleted`/`updated`/`skipped`) are summed across local + remote. The single-user reset path also honours `?panel_id=` for child routing.

### VLESS flow ↔ transport compatibility

XTLS Vision (`xtls-rprx-vision`) is only valid on raw-TCP with TLS or REALITY — it is incompatible with xhttp/ws/grpc/httpupgrade/splithttp/kcp/quic and with `security: none`. `_stream_supports_vless_flow(stream)` in `api/inbound.py` encodes that rule (`network == "tcp" and security in {tls, reality}`). Two call sites keep `Client.flow` consistent:
- `bulk-set-flow` toggles flow `""` ↔ `xtls-rprx-vision` (whitelisted by `ALLOWED_VLESS_FLOWS`); enabling on an incompatible inbound is **skipped** (counted in `skipped`), disabling is always allowed.
- `update_inbound` clears now-invalid `flow` on every client of an inbound when its transport/protocol is switched to something flow can't carry (e.g. to xhttp), before the config is regenerated.

### Bot event recovery buffer

`services/bot_events.publish` writes a `BotEvent` row to SQLite *first*, then attempts `redis.publish('bot:events', …)`. On successful publish it sets `delivered_at = now`. The `replay_undelivered_bot_events` cron (60s, runs on master and worker) re-publishes any row older than 30 seconds with `delivered_at IS NULL`. Caveat: Redis `PUBLISH` succeeding with `subscriber_count=0` (e.g. bot is down) still marks `delivered_at` because we don't check the return code — the recovery buffer protects against Redis outages but **not** consumer outages. This is intentional (a temporary bot stop is the supported way to suppress a wave of grant notifications during bulk operations).

**Two-tier dedup for node-emitted notifications.** `expiry_notification`/`traffic_notification` (emitted inline from `check_limits_and_reset`/`sync_traffic_stats`, see Traffic enforcement) get a second, content-level dedup layer on top of delivery, because the same tariff can have items on several nodes racing to warn about the same threshold. The node-local `NotificationLog` suppresses repeats from that node's own crons (per `telegram_id`/`client_id`/`kind`). `NotificationClaim` in Postgres suppresses the cross-node duplicate: its unique key is `(telegram_id, tariff_id, scope, kind)`, with `tariff_id=0` meaning "no tariff" (Postgres treats `NULL != NULL`, so a nullable column would defeat the uniqueness constraint). `scope` is empty for expiry (one grant → one warning) and `"<node>/<inbound_tag>/<email>"` for traffic, because an inbound tag like `vless-reality` can exist on every node at once. The bot claims via `POST /api/bot-service/notifications/claim` (bot service token, `claim_notification` in `services/notifications.py`) before sending either warning — an unclaimed (already-sent) notification is dropped, and a successful claim also resolves `lang`/`renewable` server-side, since the node's bare-fact payload has neither. If bot-api is unreachable the message still sends, in Russian and without a renew button (`tg_bot/bot_events_consumer.py`'s `_resolve_claim` fails open). Claims are reset on renewal by `apply_tariff_for_user` (see Provisioning) and pruned after 90 days by `cleanup_bot_events`.

### Telegram user lifecycle

- `TelegramUser` row is upserted on each `/start` via `POST /bot-service/users` (created with `language='ru'`, `language_chosen=False`, `blocked=False` by default)
- User chooses RU/EN on first start → `language_chosen=True`
- Admin can `block` a user (`POST /bot/users/<id>/block`): cancels all `UserTariffAccess` grants, disables all local `Client` rows, **removes them from Xray runtime via gRPC for vless/vmess (otherwise triggers config regen + restart)**, and disables the user's remote clients on linked panels (via the live snapshot — see Panel Federation). `unblock` re-enables clients that still have tariff time (local + remote) but does **not** restore cancelled tariffs.
- `client.telegram_id` is the link between Telegram users and Xray accounts; admin grants find the matching client by `(telegram_id, inbound_tag)` and extend in place, preserving UUIDs.

### Stream settings storage
Inbound stream settings are stored as a single JSON blob in `Inbound.stream_settings`. This blob carries extra UI-only keys beyond what Xray understands (`ssMethod`, `ssPassword`, `ssNetwork`, `authUser`, `authPass`, `wgSecretKey`, `wgPublicKey`, `wgMTU`). `generate_config_file()` strips these keys before writing the Xray config. When adding a new protocol, follow this pattern: store all metadata in the blob, strip extra keys in the stripping list at the bottom of `generate_config_file()`.

### Protocol/stream types
Protocol details live in `frontend/lib/protocols.ts` (UI-facing) and are serialized to JSON in backend models. Client IDs must be valid UUIDs for VLESS/VMess/Trojan, valid WireGuard private keys for WireGuard. Shadowsocks 2022 server/user passwords must be base64-encoded keys of the correct byte length (16 bytes for AES-128, 32 bytes for AES-256 and ChaCha20).

### Subscription links
`api/subscription.py` serves `GET /api/sub/<uuid_str>` — UUID-keyed, so renaming `Client.email` does NOT break a user's existing app config. The response can merge entries from linked panels visible to the user. Cached in Redis with a configurable TTL (`subscription_update_interval_hours` SystemSetting). `build_aggregate_sub_url(token)` builds the link the bot/dashboard show: it **prefers `SUB_DOMAIN`** (`https://<SUB_DOMAIN>/api/sub/u/<token>`) and falls back to `PANEL_DOMAIN` + `PANEL_SECRET_PATH` when `SUB_DOMAIN` is empty. The env var must be present on the **backend** container for this to take effect.

### Custom Select component
`components/ui/Select.tsx` renders a portal-based dropdown instead of a native `<select>`. It synthesizes a `React.ChangeEvent<HTMLSelectElement>` in its `onChange`. When used with react-hook-form, always spread `{...register('fieldName')}` so the `name` prop is passed — react-hook-form looks up the field by `event.target.name` and silently ignores the change if `name` is missing or empty.

### Default outbounds
On startup, `direct` (freedom) and `block` (blackhole) outbounds are auto-created if missing. These are always re-enabled if disabled — do not delete them.

### Database migrations
`panel_core.db_migration` (standalone entrypoint: `backend/migrate_db.py`) is a custom migration system (not Flask-Migrate). Current schema version is **`23`**, tracked via `PRAGMA user_version`. The script is idempotent — runs on every backend startup, uses `CREATE TABLE IF NOT EXISTS` for new tables and `ALTER TABLE ADD COLUMN` (with `_add_column_if_missing` guard) for column additions. All `ALTER`s are SQLite metadata-only (O(1)), so migration time is independent of row count. When adding a new table: add a `_ensure_<name>_table` function, call it from `migrate_sqlite_db`, bump `CURRENT_DB_VERSION`.

Bot texts have their own version: `CURRENT_BOT_TEXTS_VERSION = 17`. A bump triggers a one-shot **force-reseed** (only when `stored < CURRENT`): it DELETEs the `_REMOVED_BOT_TEXT_KEYS` tuple (purging orphan rows for keys dropped from the YAML) and then upserts every `(key, lang)` pair from `app/data/bot_texts_defaults.yaml` (~74 keys × RU/EN). The upsert **preserves admin-edited rows** — `bot_text.customized` (set to `1` whenever an admin saves a text via Bot → Texts) is honoured by `ON CONFLICT … DO UPDATE … WHERE customized = 0`, so a force-reseed refreshes only untouched defaults and never reverts customizations. On the v19 migration that added the column, rows whose stored text already diverged from the YAML default are back-filled `customized=1` to protect pre-existing edits. When you remove a key from the YAML, append it to `_REMOVED_BOT_TEXT_KEYS` (the purge ignores `customized`, since a removed key is dead regardless).

> **Reseed gotcha:** the purge/overwrite only fires when `stored < CURRENT`. An install already **at** the current number but with older content (e.g. a dev box that ran an unreleased build at the same version) is skipped — new keys still appear via the non-force `INSERT OR IGNORE` seed, but removed/changed keys don't. Coming from a real release baseline it's always clean; to force a clean reseed on such a dev box, set `system_setting.bot_texts_seeded_version` below CURRENT and restart the backend. To guarantee a reseed on *every* install regardless of prior unreleased numbers, bump strictly above the highest number any box has stored.

### Python dependencies & Docker images (uv)
Both Python services (`backend/`, `tg_bot/`) are **uv projects**: dependencies live in `[project].dependencies` in each `pyproject.toml`, pinned by a committed `uv.lock` (reproducible builds — previously every rebuild floated to latest). `[tool.uv] package = false` marks them as applications (install deps only, no wheel build), and `requires-python = "==3.12.*"` matches the `python:3.12-slim` base and the `grpcio==1.66.2` pin. There is **no `requirements.txt`** — `uv sync` is the install path everywhere.

Dockerfiles are **multi-stage**: a builder stage runs `uv sync --frozen --no-dev` into `/app/.venv` (backend also generates the Xray protobuf stubs there with `grpc_tools.protoc`), then the final stage copies only `/app/.venv` + code — no `uv` binary, no `git`, no `build-essential` in the runtime image (backend ~317 MB, bot ~176 MB). The `uv` binary comes from the pinned `ghcr.io/astral-sh/uv:0.11.19` image; `UV_LINK_MODE=copy` keeps the venv relocatable across stages, `/app/.venv/bin` is first on `PATH`, and a `.dockerignore` keeps the local `.venv` out of the build context. `UV_PYTHON_DOWNLOADS=0` forces uv to use the base image's interpreter.

CI installs uv via `astral-sh/setup-uv@v8.2.0` and runs `uvx ruff` for lint, `uv sync --frozen` + `uv run pytest` for tests. `uv sync` installs the dev group (`pytest`, `pytest-flask`) — note `pytest-flask`'s autouse fixtures pull the `app` fixture ahead of other autouse fixtures, so test mocks must patch a name **where it is used** (`app.api.inbound.restart_xray_container`), not only where it is defined (`app.services.xray.*`); the source-module patch silently misses because `api/inbound.py` did `from app.services.xray import …`.

### Statistics storage
`TrafficSnapshot` stores hourly traffic deltas per entity (user or inbound) **forever** — space is ~100 bytes × entities × 8760 hours/year, negligible for typical deployments. `DomainStat` stores daily domain hit counts and is pruned to 90 days. Both use SQLite `ON CONFLICT DO UPDATE` upserts via `literal_column()` + raw `text()` SQL — do not replace with ORM insert, it breaks atomicity.

### Secret path injection
The frontend is served under `PANEL_SECRET_PATH`. At container startup, `frontend/entrypoint.sh` injects `window.__PANEL_BASE_URL__` into `index.html` and generates `nginx.conf` from `nginx.conf.template` (which proxies `/<secret>/api/` to `backend:5000`). All traffic outside the secret path returns 404.

### gevent + gRPC
`grpc_gevent.init_gevent()` is called at app startup before any gRPC usage. The backend runs under gunicorn+gevent (single worker), so gRPC calls must be gevent-compatible. Current pin: `grpcio==1.66.2` on Python 3.12.

### ProxyFix
Configured in `app/__init__.py` as `ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)`. **Every API path is now a single proxy hop**, so `x_for=1` (trust only the right-most `X-Forwarded-For`, which Caddy sets to the real client) is correct everywhere and not spoofable:
- Panel API: caddygen routes `/<PANEL_SECRET_PATH>/api/*` (the `api_path`/`api_upstream` fields on the `panel` route in `routes.yaml`) **straight to `backend`** with the secret prefix stripped — bypassing Nginx = **1 hop**.
- Subscription domain: client → Caddy → backend = **1 hop**.
- Panel SPA (non-API) still goes client → Caddy → Nginx → static (2 hops), but those requests never use `remote_addr`.

The earlier `x_for=2` workaround (which left the sub path's `remote_addr` spoofable via a left-most XFF) is gone now that the architectural fix landed. The YooKassa webhook also re-validates against YooKassa's API rather than trusting `remote_addr`; prefer that pattern for any new webhook-style endpoint.

### Frontend tab/slider style
All horizontal tab bars use a consistent pill style: container `bg-white/[0.04] p-1 rounded-2xl border border-white/[0.05]`, active item is an absolutely-positioned `motion.div` with `layoutId` and `bg-gradient-to-br from-primary/25 to-violet-600/20 rounded-xl border border-white/[0.1] shadow-[0_0_12px_rgba(208,188,255,0.12)]`, spring transition `stiffness: 500, damping: 35`. Do not use plain CSS active classes for tab bars.

## CI Checks (run on every push)

All checks must pass before code reaches `main`. Run locally before pushing:

| Check | Command |
|---|---|
| Python lint + format | `uvx ruff check backend/ tg_bot/` · `uvx ruff format --check backend/ tg_bot/` |
| TypeScript typecheck | `cd frontend && npx tsc --noEmit` |
| ESLint | `cd frontend && npm run lint` |
| Prettier | `cd frontend && npm run format:check` |
| Frontend build | `cd frontend && npm run build` |
| Backend pytest | `cd backend && uv sync --frozen && uv run pytest tests/ -q` |
| Dockerfile lint | hadolint (runs in CI only) |

CI provisions uv via `astral-sh/setup-uv@v8.2.0` (there is no moving `v8` major tag — pin the exact version), then runs the commands above through `uvx` / `uv run`.

`uvx ruff format <dir>` and `npm run format` auto-fix formatting issues — run them before committing, not after CI fails. The `caddygen` Go tests (`cd caddy/caddygen && go test ./...`) are not in CI but should pass after caddygen changes. markdownlint is **not** run in CI.

CI **runs pytest** (the `Backend pytest` job runs `uv run pytest tests/ -q` after `uv sync --frozen`) — a test failure turns CI red and blocks `main`. Run the suite locally and confirm it's green before pushing; add tests when behavior changes — see `backend/tests/` for patterns. Watch for date-dependent tests: seed timestamps relative to the current month/day can flip near month/day boundaries.

## Git Workflow

### Feature branches — always
All work on service code (`backend/`, `frontend/`, `tg_bot/`, `caddy/`) goes in a feature branch (or the long-running `dev` integration branch), never directly on `main`.

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

**Committing directly to `main` is only acceptable for CI/config-only changes** (`.github/`, `scripts/`, `CLAUDE.md`, `README.md`, `docker-compose*.yml`) that don't touch service source files and therefore don't trigger a release.

### CI/CD skip tags
| Tag | Effect |
|---|---|
| `[skip ci]` | GitHub skips **all** workflows |
| `[skip release]` | Release job is skipped even if `versions.json` was bumped — use when restoring `versions.json` or intentionally editing it without rebuilding |

### How the release pipeline works
Release is **driven entirely by `versions.json`** on `main`. You decide what to ship by editing the file yourself — nothing auto-bumps.

1. Bump the service(s) you want to release in `versions.json` (e.g. `"bot": "2.1.4"` → `"2.2.0"`).
2. Update the matching line in `.env.example` so deployers pin the new tag (edit by hand to match the `versions.json` change; `.env.example` uses the `v`-prefixed tag, `versions.json` does not).
3. Merge to `main`. The release workflow triggers only when `versions.json` changes on `main`.
4. CI diffs the new `versions.json` against the previous commit and builds/pushes **only the services whose version string changed**. If only `xray_core_ref` changed it's a no-op; bump `backend` too to force a rebuild.
5. CI does **not** commit anything back to `main`. There is no auto-bump commit.

Force-pushing rewrites history — CI can't diff against the old SHA and falls back to `HEAD~1..HEAD`. Avoid force-pushing `main`; use feature branches.

### Panel Federation deploy ordering
When the schema bumps (any `CURRENT_DB_VERSION` change), **deploy master and all linked panels in the same wave**. A master on a newer schema may push user/tariff structures that an older linked panel can't parse. Backup first (`GET /api/backup`), then `docker compose pull && up -d` everywhere.

## Configuration

Copy `.env.example` to `.env`. Key variables:
- `PANEL_DOMAIN`, `PROXY_DOMAIN`, `PANEL_SECRET_PATH` — routing/TLS/decoy.
- `SUB_DOMAIN` *(optional)* — dedicated subscription domain. When set, subscription links are served as `https://<SUB_DOMAIN>/api/sub/u/<token>` (clean, no secret path) and `build_aggregate_sub_url` prefers it. Empty → subscriptions fall back to `PANEL_DOMAIN` + secret path. Must be in the cert's SAN and in the backend container's env.
- `SECRET_KEY`, `PANEL_ADMIN_USER`, `PANEL_ADMIN_PASSWORD`.
- `XRAY_CORE_REF` — Xray-core version to compile into the Docker image (build-time only).
- `RATELIMIT_STORAGE_URI` — Redis URI for rate limiting.
- `BOT_EVENTS_REDIS_URI` — event-bus Redis URI for the `bot:events` channel (`redis://` or `rediss://`). Defaults to `RATELIMIT_STORAGE_URI`, so master/sub/bot need not set it. **Required on a node**: its `RATELIMIT_STORAGE_URI` points at its own local Redis, so this must be pointed at the shared data-tier Redis instead (`docker-compose.postgres.yml`), e.g. `redis://node:<REDIS_NODE_PASSWORD>@<data-vm>:6379/0`. That Redis ACLs two users: `node` (publish-only into `bot:events`) and `panel` (full access, used by master/bot-api/sub/bot).
- `BACKEND_LOG_LEVEL` *(default INFO)* — backend log verbosity. Every API request (`app.requests`), scheduler job run with duration (`app.jobs`), and federation HTTP call is logged at INFO/DEBUG; `DEBUG` additionally echoes every SQL statement (`sqlalchemy.engine` + per-statement timings in `app.sql`). Slow thresholds: `BACKEND_SLOW_SQL_MS` (default 200) and `BACKEND_SLOW_REQUEST_MS` (default 1000) promote slow statements/requests to WARNING. The backend container has json-file log rotation (50 MB × 5).
- `*_IMAGE` — per-service image pins (mirrors `versions.json`).

Bot configuration is **not** in `.env`. It lives in `SystemSetting` rows managed via **Bot → Settings** in the panel UI: `bot_token`, `admin_telegram_ids`, `bot_service_token`, YooKassa `shop_id` / `secret_key`, `display_timezone`. The bot container only needs two env vars: `BACKEND_API_URL` and `BOT_SERVICE_TOKEN`. Changes take effect within ~60s without restarting the bot.

**Local vs. production validation:** When `PANEL_DOMAIN` is a local hostname (`localhost`, `*.local`, or an IP literal), the app relaxes requirements: weak `SECRET_KEY` is allowed, default `admin:admin` credentials are allowed, `memory://` rate limiting is allowed. For any real domain, all three are enforced on startup and the app refuses to start if they fail.
