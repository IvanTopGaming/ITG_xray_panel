# CLAUDE.md

Working notes for this repository. Release history lives in git; this file is only what you need to
change the code without breaking it.

## Project Overview

ITG Xray Panel — a VPN/proxy management panel for [Xray-core](https://github.com/XTLS/Xray-core):
inbounds, clients with traffic and expiry limits, routing, statistics, YooKassa billing and a
customisable Telegram bot. A master panel federates any number of remote nodes.

**Stack:** Python 3.12 · Flask · gunicorn+gevent · SQLAlchemy · Postgres/SQLite · Xray-core over gRPC ·
React + TypeScript + Vite · Aiogram 3 · Redis · Caddy (caddy-l4 SNI routing) · Docker Compose

## Commands

### Docker — one file per host role

There is no default `docker-compose.yml`; every command needs `-f`. Bring them up in this order:

```bash
docker compose -f docker-compose.postgres.yml up -d   # data tier — Postgres + Redis + pg-backup (+ offsite-backup under COMPOSE_PROFILES=offsite)
docker compose -f docker-compose.cron.yml     up -d   # cron — owns the shared schema, must migrate first
docker compose -f docker-compose.master.yml   up -d   # master, sub, bot in any order after cron
docker compose -f docker-compose.sub.yml      up -d
docker compose -f docker-compose.bot.yml      up -d
docker compose -f docker-compose.node.yml     up -d   # node — any time after the data tier
```

Images are built per role. `docker compose build backend` and `build frontend` do not work — both
Dockerfiles require a build-arg with no default.

```bash
docker buildx build --build-context project=. --build-arg PANEL_PACKAGE=panel-master \
  --tag panel-master:local --load ./backend
docker buildx build --build-context project=. --build-arg PANEL_PACKAGE=panel-botapi \
  --tag panel-bot-api:local --load ./backend
docker buildx build --build-context project=. --build-arg PANEL_PACKAGE=panel-cron \
  --tag panel-cron:local --load ./backend
docker buildx build --build-context project=. \
  --tag panel-sub:local --load -f backend/Dockerfile.sub ./backend
docker buildx build --build-context project=. --build-arg XRAY_CORE_REF=$(python3 -c "import json;print(json.load(open('versions.json'))['xray_core_ref'])") \
  --tag panel-worker:local --load -f backend/Dockerfile.worker ./backend
docker buildx build --build-context project=. --build-arg UI_PACKAGE=admin \
  --tag panel-frontend-admin:local --load ./frontend
docker buildx build --build-context project=. --build-arg UI_PACKAGE=node \
  --tag panel-frontend-node:local --load ./frontend
docker buildx build --tag panel-offsite:local --load ./offsite
```

`--build-context project=.` is the repo root; the Dockerfiles read `versions.json` from it.
`Dockerfile.worker` is separate because only the worker carries the Xray binary and the protobuf
stubs. `Dockerfile.sub` is separate because it also builds `@panel/sub-page` and bakes it into
`/app/ui`; that stage copies all of `frontend/`, which is why the repo root has a `.dockerignore`.

### Backend

```bash
cd backend
uv sync                          # deps into .venv (+ dev group)
uv run python migrate_db.py      # migrate first — an empty DB makes run.py refuse
uv run python run.py             # dev server on :5000
uv run pytest tests/
uvx ruff check backend/ && uvx ruff format backend/
```

### Frontend

npm workspace of four packages. Root scripts drive all three apps; `:admin`/`:node`/`:sub` target one.

```bash
cd frontend
npm install
npm run dev            # = dev:admin, :4200 (proxies /api → :5000);  dev:node, dev:sub (:4300)
npm run build          # tsc + vite build, all three apps
npm run typecheck      # never `npx tsc --noEmit` — the root tsconfig is deliberately inert (TS18002)
npm run lint  |  npm run format
```

### Bot / caddygen

```bash
cd tg_bot && uv sync && BACKEND_API_URL=http://backend:5000/api BOT_SERVICE_TOKEN=<token> uv run python main.py
cd caddy/caddygen && go vet ./... && go test -count=1 ./...
```

`-count=1` is required: `compose_test.go` reads compose files and `routes.yaml` from outside the Go
module, so the build cache does not notice when they change.

### Certificates — nothing to run

Caddy issues and renews everything itself over ACME. No script, no cron, no manual step. See
**TLS & Caddy** for what the deployer must still get right.

## Architecture

### Hosts and roles

Six hosts, one role each. Which role runs is decided by the gunicorn command
(`panel_core.roles.<name>:create_app`), not by `PANEL_ROLE` — that variable is a declared
expectation `bind_role()` refuses to boot against a mismatch.

| Role | Serves | Notes |
|---|---|---|
| `master` | admin API + admin SPA | no local Xray, no billing surface, no subscriptions, no backup, **no scheduler** |
| `worker` (node) | admin API + node SPA + local Xray | own SQLite (no `DATABASE_URL`), owns its schema, runs the traffic/limit/log jobs |
| `sub` | `/api/sub/*` and the subscription page | the only role serving subscriptions; a **writer** of the shared Postgres (device ledger) |
| `bot` (bot-api) | `/bot-service/*` + the whole billing surface | the three payment crons live here |
| `cron` | nothing (no ports, no blueprints) | polls nodes, replays bot events, resets grant cycles, checks releases, **owns the shared Postgres schema** |
| data tier | Postgres + Redis + `pg-backup` | outside Caddy, ports bound to a private address, own long-lived CA. Optional `offsite-backup` under the `offsite` profile — **the only thing on this host that reaches the internet** |

Also on the node: `xray-core`, `socket-proxy` (narrowed Docker socket), optional `xray-egress`.
Master/node/sub/bot each run their own `caddy`.

**Deploy order is load-bearing: data tier → cron → master, sub, bot-api.** The master refuses to
start on a virgin database and names the order in the error.

**Networks:** `panel-net` (the only one with internet egress) plus two `internal: true` segments,
`redis-net` and `dockersock-net`. Volumes worth knowing: `shared_config:/etc/xray`,
`xray_logs`, `./db_data:/app/db`, and `caddy_data:/data` — which holds the ACME account and every
certificate, so **do not delete it on upgrade**.

### Two Redis instances

`RATELIMIT_STORAGE_URI` is the box's own Redis; `SHARED_REDIS_URI` is the data tier. The rule is one
sentence: **anything more than one role has to see lives in the shared one** — the `bot:events` bus,
the node snapshots, the `panel:refresh` nudge, and subscription-cache *invalidation*. What stays
local: rate limiting and each role's own cached subscription responses (a node builds those from its
SQLite, sub builds them from Postgres — same key, two different answers).

- The shared Redis speaks **TLS only**. `validate_shared_redis_uri` refuses a cleartext `redis://`
  whose host is not on this machine; a bare service name or loopback stays plain.
- Three ACL users: `node` (publish-only into `bot:events`), `bot` (subscribe-only, used by the
  poller), `panel` (everything but `@dangerous`). A node therefore **cannot** invalidate the sub
  host's cache — it logs one line and gives up; the entry expires within `SUB_CACHE_TTL_SECONDS` (60).
- Neither instance being unreachable may refuse a request. `RATELIMIT_IN_MEMORY_FALLBACK_ENABLED`
  moves the counters into the process — it does **not** switch the limits off. Never use
  `swallow_errors`. `RATELIMIT_STORAGE_URI` is read at app-build time, not at import.
- Both clients carry a 10-second circuit breaker so an outage costs one timeout, not one per lookup.
  `new_shared_redis_subscriber()` passes `socket_timeout=None` explicitly — redis-py 8 changed the
  default and a quiet pubsub channel would otherwise drop half its messages.

### Backend layout (`backend/packages/`)

A uv workspace of **eight** distributions, all installing into the same namespace package
`panel_core`. Imports never say which wheel a module ships from.

| Distribution | Ships | Extra deps |
|---|---|---|
| `panel-core` | everything not listed below | flask stack, gunicorn, gevent, psycopg2, redis, pyjwt, requests, pyyaml, cryptography |
| `panel-adminapi` | `api/{auth,inbound,outbound,routing,statistics,system}.py` | psutil |
| `panel-worker` | `xray/{local,engine,grpc_client}.py`, `services/stats.py`, `roles/worker.py`, `api/{federation,backup}.py` | docker, filelock, grpcio, protobuf |
| `panel-master` | `api/{bot_admin,panels}.py`, `roles/master.py` | — |
| `panel-sub` | `api/subscription.py`, `roles/sub.py` | — |
| `panel-botapi` | `api/{billing,bot_service}.py`, `services/{billing,tariff_delivery,open_access}.py`, `jobs/payments.py`, `roles/botapi.py` | **yookassa** |
| `panel-cron` | `jobs/{billing,panels,grant_backfill}.py`, `roles/cron.py` | — |
| `panel-links` | `services/share_links.py` | — |

`master` and `worker` depend on `panel-core` + `panel-adminapi` and **not** on `panel-sub` — they
register no subscription blueprint. `sub` and `botapi` depend on `panel-links`.

In docs below, `app/…` is shorthand for `backend/packages/<dist>/src/panel_core/…`.

**Rules that packaging enforces, not taste:**

- **`yookassa` is `panel-botapi`-only.** Never import it — or `panel_core.services.billing` — from a
  `panel-core` module; it drags the SDK into all five images.
- **Import direction is guarded** (`tests/test_distribution_imports.py`): every `panel_core.*` import
  must resolve to a distribution inside the importer's *declared* dependency closure.
  `ALLOWED_INVERSIONS` is empty. `ROLE_DISPATCH_EXEMPTIONS` is permanent and holds only
  `dispatch.py`'s four function-level role imports.
- **`panel_core` is a PEP 420 namespace package.** No `__init__.py` anywhere in it, so importing it
  runs no code:
  - `bootstrap.py` → `bootstrap_gevent()`, `dispatch.py` → `create_app()`, `xray/facade.py` → the
    gateway shims. `from panel_core.xray import generate_config_file` raises today — a namespace
    package re-exports nothing.
  - gevent patching is each entry point's job. In containers gunicorn's `-k gevent` worker does it,
    which holds only **without `--preload`**. `build_base_app()` calls `patch_gevent_psycopg()` itself.
  - Package data goes through `panel_core/resources.py`, never a `__file__`-relative path — under an
    editable multi-distribution install (production's mode) that resolves into the wrong tree and
    fails silently.
  - `root_path` and `instance_path` are passed to `Flask` explicitly; auto-discovery raises
    `StopIteration` once more than one distribution contributes.

**Modules:**

- `app/app_base.py` + `app/dispatch.py` + `app/roles/{master,worker,sub,botapi,cron}.py` — app factories
- `app/models.py` — 22 SQLAlchemy models. **FK enforcement is OFF** (no `PRAGMA foreign_keys=ON`), so
  constraints are advisory. Exception: deleting a `LinkedPanel` or an `Inbound` app-level cascades the
  matching `TariffItem` rows via `services/tariffs.purge_tariff_items`, which also disables any tariff
  left with zero items.
- `app/extensions.py` — db, migrate, scheduler, limiter, SQLite PRAGMAs, both Redis clients
- `app/utils.py` — JWT helpers and the four auth decorators
- `app/api/` — `auth`, `inbound`, `outbound`, `routing`, `system`, `statistics` (shared);
  `panels`, `bot_admin` (master); `federation`, `backup` (node); `subscription` (sub);
  `billing`, `bot_service` (bot-api)
- `app/services/` — `xray` config generation, `traffic_store` (pure SQL, no local Xray needed),
  `stats` (worker-side collector), `panel_proxy` (federation client), `provisioning`, `billing`,
  `sub_cache`, `device_tracking`, `notifications`, `bot_events`, `expiry`, `share_links`,
  `version_check`, `bot_status`, `role_status`, `health`, `reality_health`, `egress`
- `app/jobs/` — `notifications` (replay/cleanup), `billing` (grant traffic cycles),
  `payments` (poll/refund/cleanup), `panels` (poll + refresh listener), `grant_backfill`

### Frontend (`frontend/packages/`)

Four packages; there is no root `index.html`/`vite.config.ts`/`tailwind.config.js`. No package
declares a workspace dependency on `ui-core` — each maps `@ui` → `../ui-core/src` and `@` → its own
`src` by alias, and the import direction is enforced separately by
`backend/tests/test_frontend_import_direction.py` (an alias cannot stop a relative path).

- **`ui-core`** — everything shared: `pages/` (`Dashboard`, `Routing`, `Statistics`, `System`,
  `Login`), `components/{inbound,ui,layout}`, `hooks/`, `lib/` (`api`, `types`, `protocols`,
  `panelRole`, `datetime`, …), `stores/`, `index.css`, self-hosted fonts.
- **`admin`** — `App.tsx`, `main.tsx`, `pages/{Panels,Bot}.tsx`, `components/bot/*`, `lib/bot.ts`.
- **`node`** — three files, **no page of its own**; it wires up the five shared pages.
- **`sub-page`** — the user-facing subscription page. No router, no axios, no auth store; it reads
  one endpoint, `GET /api/sub/u/<token>/info`. Carries **its own `index.css`** (ui-core's puts
  `overflow-hidden` on `body` for the admin chrome) and asserts no panel role. Its only edge into
  `ui-core` is `fonts.css`; the Tailwind theme is a duplicated copy per package.

Each admin app bakes its role at build time (`__EXPECTED_PANEL_ROLE__`) and asserts it against the
`<meta name="panel-role">` tag that `entrypoint.sh` rewrites at container start. A meta tag, not an
inline script — the CSP is `script-src 'self'`.

**Node-only and master-only surface arrives as a gate inside a shared page, never as a separate
page.** Adding one means touching every gate: the sidebar filter, the route in `admin/App.tsx`, the
route (or its absence) in `node/App.tsx`, the `enabled:` of the query, and the tab body. Miss one and
the screen looks fixed while staying unreachable. Guarded by
`test_{routing,statistics,system}_page_reaches_the_nodes.py` and `test_federation_card_is_node_only.py`.

## Key Concepts

### Auth

Four decorators in `app/utils.py`, and only four:

- `token_required` — admin JWT only. All of `bot_admin`, all of `panels.py`, `GET /api/inbounds`,
  `POST /api/federation/link-token`, `GET /api/federation/config`, plus two deliberate holdouts:
  `GET /outbounds/health` (a reachability probe is only meaningful from the box the traffic leaves
  through) and `GET /api/logs` (a stream, while `FederationClient` ends in `.json()`).
- `bot_service_token_required` — the fixed `SystemSetting('bot_service_token')`, compared in constant
  time. Opens `/bot-service/*` and `/billing/checkout` and **nothing else**.
- `federation_token_required` — a linked panel's token. The federation endpoints a master calls.
- `admin_or_federation_token_required` — admin JWT **or** federation token, nothing more. Most of
  `inbound.py`, `outbound.py`, `routing.py`, `statistics.py`, six handlers in `system.py`,
  `POST /api/user/routing`, and the node-only `backup.py`.

All of them stamp `g.auth_via` (`"admin"` / `"federation"`); `audit_privileged_change()` turns that
into a WARNING for a federated change and an INFO for the node's own admin. That log line is the only
durable record that a node's routing, settings or database were touched from the master.

JWTs last 2h and carry `pwdv` tied to `Admin.password_changed_at`, so changing the admin password
invalidates every token. The axios interceptor logs out on any 401.

**The federation token is broad and clear-text.** It reads a node's whole database (`GET /api/backup`),
its Xray config with the REALITY private key, every user's traffic and visited domains, and it can
rewrite outbounds and routing. It sits in the master's Postgres in the clear. Revoking it: on the
**node**, System → Link → *Revoke access & issue token*; then on the **master**, Panels → *Relink*.
**Never delete and re-add the panel instead** — `delete_panel` purges that panel's `TariffItem` rows
and disables any tariff left empty. Between the two steps the node is unreachable to the master
entirely; a purchase in that window stays `pending` and the 30-second poll re-applies it.

### Panel Federation

`LinkedPanel` (url + `federation_token`) is the master's row for a node; `FederationConfig` is the
singleton on the node holding the master's credentials. A node and a `LinkedPanel` are two views of
one thing, not two systems. `TariffItem.panel_id` routes a tariff item to a node.

- `poll_linked_panels` (10s, **cron service only**) is the single writer of `LinkedPanel.status` and
  of the `panel:<id>:*` Redis keys. Nothing else writes them — `relink` and *Test connection* publish
  on `panel:refresh` and let the poll do it.
- The `proxy_*` functions in `panel_proxy.py`: **mutating ones publish on `panel:refresh`, reads do
  not.** Reads run on every page load; nudging would poll the node out of band each time an admin
  looks at a list. Keep the rule to that one clause.
- A snapshot is written **twice**: `panel:<id>:snapshot` with a 60s TTL and `…:snapshot:last` with
  none (same for `last_poll`). Readers prefer the live key and fall back to the last-known copy,
  logging one WARNING per outage. Without it a cron-host outage over a minute empties every reader
  and subscriptions answer `404 "User not found"`. **Never `DEL` the snapshot key** — a missing key
  means "this panel has no remote clients", not "stale", so the reader skips the panel silently.
  Accepted cost: a client disabled during the outage keeps being served. `forget_panel` deletes all
  five keys.
- **Destructive user ops read a LIVE snapshot** (`fetch_panel_snapshot_live`), never the cache —
  `block_user`, `unblock_user`, `revoke_tariff_from_user`. Unreachable panels are surfaced in
  `panel_failures`, not skipped. Read-only screens use the cached one.
- `POST /api/federation/handshake` is the only unauthenticated route a node serves; it carries
  `30 per minute` and `Limiter` has no `default_limits`.

**The provisioning contract carries two semantics and you must send exactly one:**

- `period_ms` means *extend*: the node computes `max(now, client.expiry_time) + period_ms` itself.
  Only the node can — an orchestrator holds no `Client` row for a node-issued client, so any expiry
  it derives is wrong by exactly the remainder it cannot see.
- `expiry_ms` means *assign that exact date*. It exists for `backfill_tariff` ("give this user the
  expiry he already has elsewhere") and for admin grants.
- Both or neither → `ValueError` → 400. Do not collapse the endpoint to one field.
- **`period_ms` requires an `idempotency_key`;** `expiry_ms` must not carry one. The rule generalises:
  a key is required exactly where the operation is not idempotent on its own. The node stores the key
  with its own reply in `provision_receipt` (unique on `(idempotency_key, inbound_tag)`) and replays
  it. Two layers: a fast path that reads the receipt before mutating, and an `IntegrityError` branch
  for the concurrent request that slips past it. Removing either alone leaves the suite green.
- `ProvisionReceipt.materialized` records whether the grant reached Xray. A replay that finds it
  false synchronises first and answers only then; an existing receipt reads as unmaterialised.
- **There is no contract version, deliberately.** Compatibility comes from deploying the fleet in one
  wave. `proxy_provision` refuses a reply without `expires_at_ms` and names the panel — that is what a
  node on an older release does when handed `period_ms`, and by then it has already written `NULL`
  into that client's expiry, which makes its `check_limits_and_reset` raise on every 60s run and stops
  enforcement for everybody it serves.
- `_validate_panel_url` refuses private/loopback/`.internal` addresses unless
  `FEDERATION_ALLOW_PRIVATE_URLS` is set on the master — that check is what stops `POST /api/panels`
  being a request forwarder into the private network.

### Provisioning (`services/provisioning.py`)

`apply_tariff_for_user(telegram_id, tariff, *, source, operation_id)` is the **single gateway** for
every grant path — admin grant, trial, paid webhook, backfill. Per `TariffItem`:

- `item.panel_id` set → `proxy_provision` with **`period_ms`, never a computed expiry**.
- Otherwise extend the existing `Client` for the same `(telegram_id, inbound_tag)` — bump expiry,
  zero `up`/`down`, refresh the limit, `enable=True`, clear that client's `traffic_*`/`expiry_*`
  `NotificationLog` rows so the new cycle can warn again — or create one with a unique email.

**`expiry_time == 0` means "never" and is preserved on both branches.** Adding a period to it would
silently demote an unlimited user to a 30-day plan. `NULL` is a *different* value — a damaged row —
and is counted from now.

`operation_id` is mandatory and is a **natural key per entry point**: `pay:<payment_id>`,
`trial:<tg>:<tariff_id>`, `grant:<uuid>`, `backfill:<grant_id>`. Derive it from the payment, never
from the attempt: a multi-node tariff whose second node is down leaves the first extended, the payment
goes back to `pending`, and the cron re-runs the whole grant every 30 seconds for up to 24 hours.
The same key is the only thing between that and a user with several years of access.

`expires_at_ms` in the reply comes back **from the nodes**. Several nodes yield several dates;
`services/expiry.nearest_expiry` folds them: **`0` absorbs everything, `None` is ignored, otherwise
the nearest wins.** A plain `max()`/`min()` is wrong precisely because unlimited sorts below every
date and NULL is damage, not "never". Every surface that shows one date uses it —
`apply_tariff_for_user`'s reply, `/bot-service/users/<id>/state`, and both of the subscription role's
numbers. `backfill_tariff` deliberately keeps its own generous fold (`0` absorbs, else `max`): it
decides what to *write*, not what to show.

Each call also clears the user's `NotificationClaim` rows for that tariff, and ends in a single
`_sync_after_provision` — regenerate the config, gRPC-patch for vless/vmess or restart, invalidate
the sub-cache.

### Grants

`UserTariffAccess.billing` has two values:

- `free` — **issued access**. `access_until` is the whole of it: `NULL` = never expires, a date = ends
  there, and the date is editable.
- `paid` — **the right to buy** a private tariff. Provisions nothing.

Consequences that are the point, not side effects:

- An open-ended grant assigns `expiry_time = 0` on the node, and every layer already reads 0 as
  never — no warnings, never cut off, no code switching notifications off.
- **The tariff period drives only the traffic counter.** `next_renewal_at` means *when to zero
  `up`/`down`*, and `reset_grant_traffic_cycles` is its only reader. A tariff with no traffic limit
  gets no date at all. Being late costs nobody anything.
- **The grant wins over the key only when somebody acts on the grant** — issue, edit the term, revoke.
  Between those the panel does not touch the key, so an admin's manual extension in the Dashboard
  survives. Reconciling on a timer would remove that.
- **A holder of open-ended access is offered nothing to buy** — empty catalogue, `create_checkout`
  refuses with `open_ended_access` before any `Payment` row exists, and the trial refuses without
  burning the attempt. Otherwise somebody with unlimited permanent access could pay to be worse off.
- **A tariff with holders cannot be permanently deleted** (409). Payment history blocks it too.

### Bot billing flow

1. The bot's catalogue lists only tariffs this role can deliver (see below).
2. Bot → `POST /api/billing/checkout`. The bot **answers the Telegram callback first**, clears the
   catalogue keyboard and builds the invoice in a background task — step 3 can take ~16s.
3. `create_checkout` writes a `pending` `Payment` with a placeholder id, calls YooKassa with
   `gevent.with_timeout(8s)` + one retry on the same idempotence key, then persists the real id and URL.
4. YooKassa POSTs `https://<BOT_DOMAIN>/<BOT_WEBHOOK_PATH>/api/billing/yookassa/webhook`. The webhook
   is **unsigned**, so the body is only a trigger: the handler re-fetches the authoritative status
   before provisioning. `BOT_WEBHOOK_PATH` is about traffic, not authenticity — losing the webhook
   entirely is survivable, the 30s poll confirms.
5. `apply_payment`: idempotency fast-path → **atomic claim** `UPDATE payment SET status='processing'
   WHERE id=:id AND status='pending'` → revalidate the tariff → `apply_tariff_for_user` with
   `operation_id=f"pay:{payment.id}"` → `succeeded` + publish `payment_succeeded` carrying the expiry
   **the nodes reported**. On exception the claim goes back to `pending`.

**Never widen that claim to `IN ('pending','processing')`.** It would close the stranded-payment gap
in one line and reopen the double-grant it exists to prevent. Recovery is a separate branch:
`release_stranded_claims()` (called first by `poll_pending_payments`) returns a row left in
`processing` by a dead process. How long it has been held is measured **in the process**, not in the
row — `created_at` is the wrong clock because the poll reaches back 24 hours.

**A tariff this role cannot deliver is refused before an invoice exists.**
`services/tariff_delivery.is_deliverable()` is false for a tariff with no items, and for one with
**any** item whose `panel_id` is `NULL` on a role with no local Xray. Wired into three places —
`_ensure_tariff_available`, the catalogue, and the trial's `_deliverable_trial_tariff` — and the check
is **per item**: a tariff with two node items and one orphan is refused whole.

`cleanup_old_payments` asks YooKassa before cancelling anything: `succeeded` → apply,
`waiting_for_capture` → leave, unreachable → leave and retry. It must never cancel on local state
alone. Refunds have **no webhook path in practice** — `reconcile_refunds` is a sampling job (hourly,
last 30 days, capped at 200 most recent) and is the only thing that revokes access after a refund.

### Traffic, limits and statistics

`stats.py` polls per-user up/down over gRPC every 10s into `Client.up/down` and 10-minute
`TrafficSnapshot` buckets; `check_limits` (60s) disables users past their limit or expiry; monthly
per-client resets zero the counters **and** delete that client's `traffic_*` `NotificationLog` rows.
Both jobs emit their notifications inline — there is no separate notification cron.

**`TrafficSnapshot` and `DomainStat` live on a node and only on a node.** Their only writers are jobs
`roles/worker.py` registers, so the master's copies are empty by construction. All five
`/api/stats/*` handlers therefore dispatch `?panel_id=` to the node **before** consulting
`has_local_xray()`, and a master with no node named answers **501** naming `panel_id` — not 200 with
zeroes, because a zero and a real answer differ only in the body.

`_top_domains_sql` is **dialect-aware**. `INDEXED BY ix_ds_date_domain_cover` is required on SQLite
(the planner otherwise picks the narrower index and stops covering the query) and is a syntax error
on Postgres. Emit it only when the bound dialect is sqlite; an undeterminable dialect falls back to
portable SQL. Both halves are load-bearing. The upserts use raw `text()` + `ON CONFLICT DO UPDATE` —
do not replace them with ORM inserts, it breaks atomicity.

### Xray control from the master

The same shape as statistics, and the **order is the whole point**: `?panel_id=` is dispatched
*before* `has_local_xray()`. Hoisting the gate above the dispatch turns the master side back into 501
and is invisible on a node, where both orders behave identically. Six handlers:
`GET`/`PUT /api/system/settings`, `GET /api/config`, `POST /api/system/update-geo`,
`POST /api/restart`, `POST /api/user/routing`.

`GET /api/logs` is deliberately **not** in that list — it is a stream, and both `FederationClient`
methods end in `.json()`. It stays the one `hasLocalXray` block left on the System page.

`xray_log_level`, `geoip_url`, `geosite_url` are `SystemSetting` rows whose only reader is
`generate_config_file()` on a node, against that node's own SQLite. The federation snapshot carries
`preferred_outbound` so the master can show a node client's current route.

### Xray integration

`services/xray.py` / `xray/engine.py` writes the full JSON to `/etc/xray/config.json` and manages live
users over the Handler/Stats gRPC API. The file lock `/etc/xray/config.lock` serialises writers; the
config is written to a candidate path, validated with `xray run -test`, then renamed. Config
regeneration and restart happen together when inbounds or outbounds change. `grpc_gevent.init_gevent()`
runs before any gRPC use; current pin `grpcio==1.66.2` on Python 3.12.

**Stream settings are one JSON blob** on `Inbound.stream_settings`, carrying UI-only keys beyond what
Xray understands (`ssMethod`, `ssPassword`, `ssNetwork`, `authUser`, `authPass`, `wgSecretKey`,
`wgPublicKey`, `wgMTU`). `generate_config_file()` strips them at the bottom. New protocol → store
metadata in the blob, add the key to that stripping list.

**XTLS Vision is only valid on raw-TCP with TLS or REALITY.** `_stream_supports_vless_flow` encodes
it (`network == "tcp" and security in {tls, reality}`). `bulk-set-flow` skips incompatible inbounds
(counted in `skipped`); `update_inbound` clears now-invalid `flow` on every client when the transport
changes.

**A REALITY inbound on `:443` must have `serverNames[0] == PROXY_DOMAIN`.** Caddy routes `:443` by
SNI and learns the decoy from that variable alone; when the two drift, every client is handed to the
panel instead of Xray and simply never connects, with nothing reporting a fault. `api/inbound.py`
refuses to save such an inbound. `services/reality_health.py` counts refused handshakes from the
node's error log and publishes the number in the snapshot — a few mean scanners, a steady stream with
nobody connecting means the decoy cannot serve as a REALITY target.

**Default outbounds:** a node auto-creates and re-enables `direct` and `block` at start-up — do not
delete them there. The master does the opposite: `bootstrap_defaults(app, system_outbounds=False)`
also *deletes* those two rows, because they sit in the Postgres of every panel upgraded from an
earlier release. **The flag lives at the call site**, not inside `bootstrap_defaults` — both roles
call the same function, so switching the seed off in the shared body would silently disarm every node.

### Subscription links

`api/subscription.py` serves `GET /api/sub/<uuid>` (UUID-keyed, so renaming an email does not break a
user's app) and `GET /api/sub/u/<token>` (per Telegram account). Responses merge entries from linked
panels, cached in Redis.

- **Only the `sub` role serves subscriptions at all.** `SUB_DOMAIN` is therefore load-bearing:
  `build_aggregate_sub_url` / `build_client_sub_url` return `None` when it is empty, and there is no
  `PANEL_DOMAIN` fallback. All four service hosts demand it via `:?` and only one serves it — the
  master and each node build the links their Dashboard hands out, bot-api builds the ones the bot sends.
- The per-UUID route builds a node client's config **from the snapshot**, not by calling the node — a
  dead node used to stall a live user for eight seconds. Cost: `subscription-userinfo` counters are up
  to `SUB_CACHE_TTL_SECONDS` + poll-interval stale, against an announced `profile-update-interval` of
  24h. A missing snapshot is still a 404 there; nothing else can answer it.
- One route, two audiences: a client app's User-Agent gets the raw config, a browser gets the React
  page baked into `panel-sub` at `/app/ui` (override `SUB_PAGE_DIST`), which fetches
  `GET /api/sub/u/<token>/info`. A missing bundle 503s the page **without** touching config delivery.
- **An expired or blocked subscription answers `200` with an explanation**, not 404: a single entry
  named "подписка закончилась — продлите в @bot" pointing at `127.0.0.1:1` with a random UUID, and a
  real past `expire=`. A client app renders 404 as a failed update and cannot tell an expiry from a
  reset link. **An unknown token still answers 404** — otherwise revoking a leaked link would look
  like an expiry and probing random tokens would get a meaningful reply. The bot handle comes from
  `SystemSetting('bot_username')`, which the bot reports on its 30s runtime-config poll.

### Device limit

`UserDevice` is unique on `(telegram_id, hwid)`; `device_tracking.user_device_gate()` registers or
refreshes a row on every config request and answers `limit` once the account is over
`device_limit_per_user`. It runs on the **sub** role only — which makes sub a **writer** of the shared
Postgres, so a read-only credential there breaks the hot path.

**Nothing joins through `Client`.** The ledger stores `telegram_id` and nothing else identifying: a
join would give a user one budget per node, or zero on sub, where no node-issued `Client` row exists.
A client with no `telegram_id` (admin-created) has no device tracking at all. The
`Client.device_limit` / `Inbound.device_limit` **columns** survive but nothing reads, accepts or
returns them.

### Bot events

`services/bot_events.publish()` writes a `BotEvent` row **first**, then publishes to the shared Redis
channel `bot:events`, stamping `delivered_at` on success. `replay_undelivered_bot_events` (60s, on
cron over Postgres and on every worker over its own SQLite) re-publishes anything older than 30s with
no `delivered_at`.

Caveat, and it is intentional: `PUBLISH` succeeding with `subscriber_count=0` still stamps delivered.
The buffer protects against Redis being down, **not** against the bot being down — a temporary bot
stop is the supported way to suppress a wave of notifications during bulk operations.

**Never publish an event the consumer has no branch for.** It only fills the table until the cleanup
cron prunes it. Guarded by `test_events_without_a_consumer.py`, which also asserts a positive control
because "no rows" is what a broken publisher looks like too.

**Two-tier dedup for node-emitted warnings.** The node-local `NotificationLog` suppresses repeats from
that node's own crons. `NotificationClaim` in Postgres suppresses the cross-node duplicate: unique on
`(telegram_id, tariff_id, scope, kind)`, with `tariff_id=0` meaning "no tariff" (Postgres treats
`NULL != NULL`, so a nullable column would defeat the constraint). `scope` is empty for expiry and
`"<node>/<inbound_tag>/<email>"` for traffic. The bot claims via
`POST /bot-service/notifications/claim` before sending; the claim also resolves `lang` and `renewable`,
which the node's bare-fact payload cannot know. If bot-api is unreachable the message still sends, in
Russian and without a renew button.

### Telegram bot

`tg_bot/` is a backend client with no local state. **One token may only long-poll once** — never start
a second poller with the same token.

**Every user-facing screen is built from one response, `GET /bot-service/users/<id>/state`**, which
carries per client `up`/`down`/`limit_bytes`/`expiry_time`/`enable`/`inbound_label`/`panel_name`, a
`links` array of ready share links, plus the account's `sub_url` and `expires_at_ms`. Do **not** add a
path from the bot to the admin API to fill any of it in — bot-api serves none of those routes, and
that is exactly how all three screens broke at once once before.
`tg_bot/tests/test_no_admin_surface.py` fails on any module reaching for one.

`links` is empty for a client with no `panel_id`, and that is correct: bot-api can only build a link
for a client it sees in a node snapshot, where the hostname comes from `LinkedPanel.url`.

`runtime_config` re-polls `GET /api/bot/runtime-config` every 30s and hot-swaps the token or proxy by
rebuilding the aiogram session without restarting the events consumer (which holds a Bot accessor
closure, not a fixed ref). A 401 there is logged at **ERROR**, once per outage, naming
`BOT_SERVICE_TOKEN` — rotating it stops the bot until the bot host's `.env` is updated and restarted,
deliberately with no grace period.

### Database migrations

**Exactly one service migrates each database.** The shared Postgres: the **cron service** and nothing
else. A node's SQLite: that node. Master, sub and bot-api migrate nothing — the master seeds defaults
into an existing schema and calls `_require_schema()` first, refusing to start on a virgin database.

`panel_core.db_migration` (entrypoint `backend/migrate_db.py`) is a custom system, not Flask-Migrate.
Schema version **27** via `PRAGMA user_version`; idempotent, `CREATE TABLE IF NOT EXISTS` plus guarded
`ALTER TABLE ADD COLUMN` (all metadata-only, so migration time is independent of row count). New
table → add `_ensure_<name>_table`, call it from `migrate_sqlite_db`, bump `CURRENT_DB_VERSION`.
Retired tables are listed once in `RETIRED_TABLES` and dropped by both paths from that list —
`create_all()` never removes a table, so a retirement missing from that tuple lingers forever.

`migrate_postgres_db` (`pg_migrate.py`) is `create_all()` + `_add_missing_columns()` + dropping FK
constraints + `schema_version` + seeding bot texts. Two boundaries are deliberate: it **never drops**
a column the models no longer declare, and a `NOT NULL` column **without a `server_default`** is added
*nullable* with a WARNING rather than failing — an existing table cannot be back-filled from here and
a start-up refusal on cron would take the whole schema with it. **Give every new column a
`server_default` if you want it `NOT NULL` in Postgres.**

`PG_DEAD_TABLES` (`traffic_snapshot`, `domain_stat`, `notification_log`) are excluded from
`create_all` and dropped — but only when the caller passes `drop_dead_tables=True`, and only
`roles/cron.py` does (the flag lives at the call site because a worker pointed at Postgres genuinely
writes them). **A dead table holding rows is never dropped**; cron logs its row count instead.

**Bot texts have their own version, `CURRENT_BOT_TEXTS_VERSION = 20`.** A bump triggers a one-shot
force-reseed *only when `stored < CURRENT`*: it DELETEs `_REMOVED_BOT_TEXT_KEYS` and upserts every
`(key, lang)` from `app/data/bot_texts_defaults.yaml`, honouring `bot_text.customized` so admin edits
survive. **When you remove a key from the YAML, append it to `_REMOVED_BOT_TEXT_KEYS`** — nothing else
ever deletes a bot text, and an orphan row stays editable on every live database forever.
A purely additive key needs no version bump.

> Reseed gotcha: an install already **at** the current number but with older content (a dev box that
> ran an unreleased build) is skipped. Set `system_setting.bot_texts_seeded_version` lower and restart.

### TLS & Caddy

`caddy/caddygen/` reads `caddy/routes.yaml` + env at container start and emits Caddy's **native
JSON** (`caddygen → caddy validate → caddy run`, so a bad config fails fast). The caddy-l4 app owns
`:443` and routes by **TLS SNI**:

- `PROXY_DOMAIN` (decoy) → raw-TCP passthrough with PROXY-protocol to `xray:443`, so Xray sees the
  real REALITY handshake.
- `PANEL_DOMAIN` / `SUB_DOMAIN` / `BOT_DOMAIN` → TLS terminated, PROXY-protocol'd to a per-route
  loopback HTTP server (security headers + CSP, optional path allowlist) → `frontend:80` / `backend:5000`.
- A plain `:80` server 308-redirects everything to https.

`routes.yaml` fields: `match` (SNI, `${ENV}`-interpolated), `upstream`, `tls`, `only_paths` (→404,
implies `tls`, also interpolated), `strip_prefix`, `api_path`/`api_upstream`. A route whose `match`
interpolates to empty is **dropped**; interpolated paths are collapsed (`//` → `/`).

**ACME, four things each load-bearing:**

- Only routes with `tls: true` become subjects. The decoy is somebody else's domain, so requesting it
  can only fail — and Let's Encrypt counts *failed* validations against the account.
- A local hostname (`localhost`, `*.local`, a bare IP, any name without a dot) gets Caddy's
  `internal` issuer instead.
- The `:80` server keeps `automatic_https: {disable: true}` and that does **not** block the challenge:
  Caddy answers HTTP-01 in `Server.ServeHTTP` before any user route. What matters is only that
  something still listens on `:80`.
- TLS-ALPN-01 is unavailable by construction (layer4 owns `:443`), so **`:80` must be reachable from
  the internet** on all four TLS hosts and the domain must resolve to that box.

`ACME_EMAIL` (expiry warnings) and `ACME_CA` (point at LE **staging** while rehearsing) are optional.

**Each Caddy must receive only its own domains, and that is a property of the compose file, not of
`routes.yaml`.** caddygen drops a route only when its variable interpolates to the empty string; it
has no notion of a host role, so every domain variable the container can see turns another route on.
`${VAR:-}` does **not** help — compose substitutes whenever the variable is present. Therefore no
`caddy` service declares `env_file`, and each lists exactly what its routes need: master
`PANEL_DOMAIN` + `PANEL_SECRET_PATH`; node those plus `PROXY_DOMAIN`; sub `SUB_DOMAIN` alone; bot
`BOT_DOMAIN` + `BOT_WEBHOOK_PATH`. SNI is client-chosen and each box serves its cert for whatever name
is asked, so a stray variable makes the wrong host answer for the wrong domain. Guarded by
`test_compose_host_ingress.py` and `caddygen/compose_test.go`.

### Bulk user operations

`POST /users/bulk-{delete,enable,adjust-days,adjust-traffic,set-flow}` and `/users/reset-traffic`.
Each carries `users: [{tag, email, panel_id?}]`; `_split_users_by_panel` splits local from per-panel
remote, and remote groups are forwarded to the owning node's **identical** endpoint with `panel_id`
stripped, so the child runs them purely locally and there is no recursion. Proxying is best-effort —
an erroring child lands in `errors[]` rather than failing the batch, and counts are summed.

### Backup

`GET /api/backup` + `POST /api/restore` ship from `panel-worker` and are registered only by
`roles/worker.py`, so every other role answers 404. Both copy a SQLite file. A cheap `is_postgres()`
refusal (409, naming `pg-backup`) guards the handlers themselves, because `docker-compose.node.yml`
merely omits `DATABASE_URL` rather than forbidding it. **The shared database is backed up by the
`pg-backup` container, never through the panel.** A node is backed up from its card on Panels, which
streams through the master straight into the browser and stores nothing.

**Off-site copies are a profile, not a service.** `offsite-backup` exists only when
`COMPOSE_PROFILES=offsite`, and turning it on is the one thing that gives the data tier outbound
network access. It loops `scripts/offsite_backup.sh` out of `panel-offsite` (Alpine + a pinned rclone
binary + `psql`): `rclone copy` the new dumps, `rclone delete --min-age ${OFFSITE_KEEP_DAYS}d` for a
remote rotation that is **independent of the local one** (90 days local, 365 remote), then an upsert
of three `system_setting` rows. Three things are load-bearing and none of them is visible in the
container's logs:

- **`copy`, never `sync`.** `sync` would delete on the far side everything local rotation has
  pruned, silently collapsing remote depth to local depth.
- **`./rclone` is mounted writable.** rclone writes the refreshed OAuth token back into its own
  config; a read-only mount works until the access token finally expires, and then the uploads stop
  with nothing reporting a fault. `./pg_backups` is mounted `:ro` for the mirror reason.
- **A failed pass must not kill the container.** The loop carries `|| true`. Diagnosis is the age of
  the mark, shown on the master's System → About card and red after three intervals — never a
  restart loop, which is a symptom rather than a diagnosis.

The panel's whole knowledge of it is `services/offsite.read_status()` reading
`offsite_backup_last_success_ms`, `offsite_backup_interval_seconds` and `offsite_backup_remote`.
Those three strings cross a container boundary that nothing else connects: rename one on either side
and the card reports "never recorded" forever while the uploads carry on fine.
`test_a_backup_that_stopped_leaving_becomes_visible.py` is the only guard on that seam. The reading
answers `applicable: False` on a role whose database is not the shared Postgres, and the line is
gated on `!isWorker` in the bundle — a node runs no such container and its absence means nothing
there. The dump is worth more than `.env` (bot token, YooKassa credentials, every node's federation
token, every `sub_token`), so the installer offers a `crypt` wrapper; the passphrase is printed once
and stored nowhere.

### Odds and ends

- **Error handling:** raise `ValueError` for anything the user can fix — it becomes a 400 with the
  message shown. A bare `Exception` becomes a 500 with a generic message.
- **ProxyFix** is `x_for=1, x_proto=1, x_host=1, x_prefix=1`. Every API path is a single proxy hop:
  the panel API goes Caddy → `backend` directly (the SPA's own 2-hop path never reads `remote_addr`).
  Prefer the webhook's re-validation pattern over trusting `remote_addr` for anything new.
- **Secret path:** `frontend/entrypoint.sh` rewrites `<base href>` and the `panel-role` meta tag with
  `sed`, then renders `nginx.conf` from the template. Everything outside `PANEL_SECRET_PATH` is 404.
  No inline script anywhere on that path — the CSP is `script-src 'self'`.
- **`Select.tsx`** is a portal-based dropdown that synthesizes a `ChangeEvent<HTMLSelectElement>`.
  With react-hook-form always spread `{...register('field')}` — RHF looks the field up by
  `event.target.name` and silently ignores a change when `name` is missing.
- **Tab bars** use one pill style: container `bg-white/[0.04] p-1 rounded-2xl border border-white/[0.05]`,
  active item an absolutely-positioned `motion.div` with `layoutId`, spring `stiffness: 500, damping: 35`.
  Never plain CSS active classes.
- **Egress:** the panel allocates internal bind-IPs from `EGRESS_BIND_POOL_RANGE`, but
  `xray-egress/sync.sh` hardcodes `172.28.0.128–254` in its cleanup branch. Change the variable and
  the sidecar will still add addresses while never removing stale ones. Host-side state
  (`egress.conf`, `egress-owned`, `egress-sync.sh`, the systemd timer) deliberately lives beside
  `.env` rather than in it, because no container reads it.

## Background jobs

| Job | Interval | Role | What it does |
|---|---|---|---|
| `sync_traffic` | 10s | worker | per-user gRPC counters → `Client` + `TrafficSnapshot`; emits traffic warnings at 80/95/100% |
| `check_limits` | 60s | worker | disables expired/over-limit clients; emits expiry warnings at 3d/1d/1h/expired |
| `parse_logs` | 15s | worker | tails the access log into `DomainStat`; also counts refused REALITY handshakes |
| `cleanup_stats` | 24h | worker | prunes `DomainStat` older than 90d |
| `poll_linked_panels` | 10s | cron | polls every node; also listens on `panel:refresh` for out-of-band polls |
| `reset_grant_traffic_cycles` | 15m | cron | zeroes a grant's traffic counters; **provisions nothing, touches no expiry** |
| `check_latest_version` | 6h | cron | fetches the published `versions.json` and persists it into `SystemSetting` |
| `replay_undelivered_bot_events` | 60s | cron + worker | re-publishes undelivered `bot_event` rows |
| `cleanup_bot_events` | 24h | cron + worker | prunes delivered >7d, undelivered >30d, claims and receipts >90d |
| `poll_pending_payments` | 30s | bot-api | releases stranded claims, then reconciles pending YooKassa payments (30s–24h old) |
| `reconcile_refunds` | 1h | bot-api | re-checks succeeded payments ≤30d (cap 200) and revokes on refund |
| `cleanup_old_payments` | 24h | bot-api | asks YooKassa, then cancels genuinely dead pendings; deletes terminal rows >90d |

The master registers **no scheduler at all**.

## Configuration

Each host copies its own example — `.env.{master,node,sub,bot,cron,data}.example` — onto that box and
nowhere else. There is no shared `.env.example`; one file cannot be correct for every host
(`RATELIMIT_STORAGE_URI` alone means two mutually exclusive things). `test_env_examples.py` enforces
both directions: every `${VAR:?}` a compose file demands is in that host's example, and no example
defines a variable its compose file never references. `test_env_reaches_code_that_reads_it.py`
resolves each service to its image's dependency closure and fails on a variable nothing inside can read.

- `PANEL_DOMAIN` — **per-host by design.** On a node it must be *that node's* domain; it is also the
  node's identity in bot events. On sub and bot it routes nothing and is read only as the "is this a
  real deployment" marker (weak `SECRET_KEY` and non-`verify-full` `DATABASE_URL` are refused for a
  non-local domain), plus, on sub, as the default server address for an inbound with no explicit host.
- `PANEL_SECRET_PATH` — master and node only. Read by `api/federation.py`, the frontend entrypoint
  and Caddy's route.
- `PROXY_DOMAIN` — node only; the decoy SNI, and the value a REALITY inbound on `:443` must match.
- `SUB_DOMAIN` *(required — subscriptions do not work without it)* — set on all four service hosts,
  served by one. No fallback.
- `BOT_DOMAIN`, `BOT_WEBHOOK_PATH` — bot host.
- `SECRET_KEY`, `PANEL_ADMIN_USER`, `PANEL_ADMIN_PASSWORD` — the last two are mapped into the
  container as `PANEL_USER` / `PANEL_PASSWORD`.
- `DATABASE_URL` — master, sub, bot, cron. Must carry `sslmode=verify-full` **and**
  `sslrootcert=/etc/ssl/panel-ca.crt` (libpq otherwise looks for `~/.postgresql/root.crt`, which no
  container has). A node sets none and falls through to its own SQLite.
- `RATELIMIT_STORAGE_URI` / `SHARED_REDIS_URI` — see **Two Redis instances**. Both mandatory via `:?`
  on the roles that read them. The bot container takes `BOT_SHARED_REDIS_URI` (the subscribe-only
  credential), mapped into its `SHARED_REDIS_URI`.
- `POSTGRES_BIND` / `REDIS_BIND` — data tier; both default to `127.0.0.1` so an unset value cannot
  publish the tier to the internet.
- `OFFSITE_IMAGE` selects the container; `OFFSITE_REMOTE`, `OFFSITE_INTERVAL_SECONDS` (1800) and
  `OFFSITE_KEEP_DAYS` (365) are read by `scripts/offsite_backup.sh` — all four data tier only.
  `COMPOSE_PROFILES=offsite` is what turns the service on and is read by the **compose CLI**, not by
  the YAML — which is why `test_env_examples.py` exempts it by name. None of the four may use
  `${VAR:?}`: compose interpolates the whole file before it filters by profile, so a required
  reference inside a profiled service refuses the `up` on every data tier that never wanted off-site
  copies.
- `BACKUP_INTERVAL_SECONDS` (7200) / `BACKUP_KEEP` (1080) — the local dump cadence and depth, per
  `.env.data.example`; compose's own fallback for an existing `.env` that predates the two
  variables is 21600 / 14. The interval is substituted host-side in the entrypoint; only
  `BACKUP_KEEP` reaches the container.
- `FEDERATION_ALLOW_PRIVATE_URLS` — master only, off by default. Only `1`/`true`/`yes`/`on` count.
- `BACKEND_LOG_LEVEL` (default INFO), `BACKEND_SLOW_SQL_MS` (200), `BACKEND_SLOW_REQUEST_MS` (1000).
  `DEBUG` additionally echoes every SQL statement. Containers rotate json-file logs at 50 MB × 5.
- `*_IMAGE` — one pin per service, mirroring `versions.json`: `MASTER_IMAGE`, `WORKER_IMAGE`,
  `SUB_IMAGE`, `BOT_API_IMAGE`, `CRON_IMAGE`, `BOT_IMAGE`, `FRONTEND_ADMIN_IMAGE`,
  `FRONTEND_NODE_IMAGE`, `CADDY_IMAGE`, `XRAY_EGRESS_IMAGE`.

**Bot configuration is not in `.env`.** It lives in `SystemSetting` rows edited under Bot → Settings:
`bot_token`, `admin_ids`, `bot_service_token`, YooKassa credentials, `display_timezone`, and — the
panel's own, not the bot's — `brand_name`, `subscription_update_interval_hours` (both read by the
**sub** role) and `panel_name` (read by the **master** when linking a node). The bot container needs
only `BACKEND_API_URL` and `BOT_SERVICE_TOKEN`; changes take effect within ~60s without a restart.

**Local vs production:** when `PANEL_DOMAIN` is local (`localhost`, `*.local`, an IP literal) the app
relaxes three checks — weak `SECRET_KEY`, default `admin:admin`, `memory://` rate limiting. For any
real domain all three are enforced at start-up and the app refuses to boot.

## Scripts

- `scripts/install.sh` — the installer and manager: `install`, `doctor`, `update`, `reconfigure`,
  `egress`. The data tier prints a bundle carrying every shared secret and its CA; the other five
  hosts derive their whole `.env` from it. This is what a deployer pipes into bash as root, so it is
  linted in CI.
- `scripts/egress-sync.sh` — host-side synchroniser for dedicated outgoing IPs, run by a systemd
  timer. Three behaviours are load-bearing: an unreachable panel leaves the host untouched (exit 1)
  while an empty plan clears it (exit 0); a settled host issues no command under `--dry-run`; and the
  jump into `EGRESS_SNAT` is re-asserted as **first** in `POSTROUTING`, because Docker rewrites that
  chain and its MASQUERADE landing ahead of ours silently sends every dedicated address out on the
  primary IP.
- `scripts/pg_backup.sh` — what the `pg-backup` container runs on `BACKUP_INTERVAL_SECONDS`, keeping
  `BACKUP_KEEP` dumps.
- `scripts/offsite_backup.sh` — one off-site pass, POSIX `sh` (the image is Alpine). Copies, rotates
  the remote by age, records the success. Bind-mounted rather than baked in, like `pg_backup.sh`.

## CI

Run before pushing — all of these gate `main`:

| Check | Command |
|---|---|
| Python lint + format | `uvx ruff check backend/ tg_bot/` · `uvx ruff format --check backend/ tg_bot/` |
| TypeScript | `cd frontend && npm run typecheck` |
| ESLint / Prettier | `npm run lint` · `npm run format:check` |
| Frontend build | `npm run build` |
| Backend tests | `cd backend && uv sync --frozen && uv run pytest tests/ -q` |
| Bot tests | `cd tg_bot && uv sync --frozen && uv run pytest tests/ -q` |
| caddygen | `cd caddy/caddygen && go vet ./... && go test -count=1 ./...` |
| Shell | `shellcheck --severity=warning $(git ls-files '*.sh')` |
| Dockerfiles | hadolint (CI only) |

CI provisions uv via `astral-sh/setup-uv@v8.2.0` — pin the exact version, there is no moving `v8` tag.
`uvx ruff format` and `npm run format` auto-fix; run them before committing, not after CI fails.
markdownlint is not run.

Add tests when behaviour changes; `backend/tests/` has the patterns. Watch for date-dependent tests —
timestamps seeded relative to the current month or day flip near boundaries.

`backend/tests/conftest.py` stubs the gRPC modules in `sys.modules` before importing the app, so the
suite runs on a dev checkout without the protobuf bundle that ships only inside the image. That stub
is global, which would make any in-process check that the light roles import without
`grpcio`/`protobuf`/`docker`/`filelock` pass vacuously — `test_light_role_import_isolation.py`
asserts it in a **separate subprocess** instead.

## Git workflow

All work on `backend/`, `frontend/`, `tg_bot/` or `caddy/` goes in a feature branch, never directly on
`main`. Open a PR and merge with **Squash and merge**. Committing straight to `main` is acceptable
only for CI/config-only changes (`.github/`, `scripts/`, `CLAUDE.md`, `README.md`, `docker-compose*.yml`)
that touch no service source and therefore trigger no release.

| Tag | Effect |
|---|---|
| `[skip ci]` | skips every workflow |
| `[skip release]` | skips the release job even when `versions.json` changed |

### Releases

Driven entirely by `versions.json` on `main`; nothing auto-bumps.

1. Bump the services you want to ship.
2. Update the matching line in every `.env.<host>.example` that pins that image (the examples use the
   `v`-prefixed tag, `versions.json` does not). `CADDY_IMAGE` is pinned in all four host files;
   `test_image_targets.py` checks every file that declares a pin.
3. Merge to `main`. CI diffs `versions.json` against the previous commit and builds only what changed.
   If only `xray_core_ref` moved it is a no-op — bump `worker` too, it is the only image that ref affects.
4. CI commits nothing back.

Avoid force-pushing `main`: CI then cannot diff against the old SHA and falls back to `HEAD~1..HEAD`.

**Rebuild fan-out.** `panel-core` → all five backend images. `panel-adminapi` → master + worker.
`panel-links` → sub + bot-api. `panel-sub`, `panel-master`, `panel-worker`, `panel-botapi`,
`panel-cron` → one each. That fan-out is the blast radius — what depends on the distribution, not a
mandate that every release touching it bumps every image on the list. Narrow the bump to the role(s)
where a `panel-core` change is actually observable: `services/offsite.py` and its use in
`services/health.py` are reached only through `/system/health`, which `panel-adminapi` — and only
`panel-adminapi` — registers, so `sub`, `bot-api` and `cron` never execute either module at all; and
on `worker` the read short-circuits on `is_postgres()` before doing anything a SQLite-backed role
could observe. Nothing changes for four of the five images, so `master` is the only one whose
bump this work requires.

A change confined to `frontend/packages/sub-page/**` is a **backend** release
(bump `sub`). A change to `ui-core/fonts.css` or the `.woff2` files is a **three-image** release
(`sub` + both frontends) — that one file is sub-page's only edge into `ui-core`; any other `ui-core`
change rebuilds the two frontend images alone.

`offsite/**` → `offsite` alone. That image carries no panel code, so nothing else rebuilds with it.
`scripts/offsite_backup.sh` is bind-mounted, not baked in, so a change to it alone needs **no** image
bump — but it also ships through **no** automated path: `install.sh update` only moves image pins and
does not re-fetch bind-mounted scripts. A script-only fix reaches a live data tier only by fetching the
file by hand (or a fresh `install.sh install` into a new directory) — do not assume `install.sh update`
picks it up.

**When `CURRENT_DB_VERSION` changes, deploy the master and every linked panel in one wave.** Back up
first — the data tier with `pg-backup`, each node from its card on Panels. Backwards compatibility is
offered within minor releases only; there is no version negotiation on the federation contract.
