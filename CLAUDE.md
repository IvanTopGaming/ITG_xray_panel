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
docker compose build bot      && docker compose up -d bot
docker compose build caddy    && docker compose up -d caddy
```

The backend is split into four per-role images and `docker compose build backend` no longer
works — the shared `backend/Dockerfile` now requires a `PANEL_PACKAGE` build-arg with no default,
so a plain build fails with `PANEL_PACKAGE build-arg is required`. It builds only `panel-master`
and `panel-bot-api`; `panel-worker` and `panel-sub` each have their own file. Build the role you
need directly with the same invocation CI uses:
```bash
docker buildx build --build-context project=. --build-arg PANEL_PACKAGE=panel-master \
  --tag panel-master:local --load ./backend
docker buildx build --build-context project=. --build-arg PANEL_PACKAGE=panel-botapi \
  --tag panel-bot-api:local --load ./backend
docker buildx build --build-context project=. \
  --tag panel-sub:local --load -f backend/Dockerfile.sub ./backend
docker buildx build --build-context project=. --build-arg XRAY_CORE_REF=$(python3 -c "import json;print(json.load(open('versions.json'))['xray_core_ref'])") \
  --tag panel-worker:local --load ./backend -f backend/Dockerfile.worker
```

`backend/Dockerfile.sub` takes no `PANEL_PACKAGE` — it hardcodes `--package panel-sub`, because it
also carries a `node:20-alpine` stage that builds `@panel/sub-page` and bakes the result into
`/app/ui`. That stage copies all of `frontend/` out of the `project` context, which is why the repo
root carries a `.dockerignore`: without it the host's `frontend/node_modules` (212 MB) is shipped
into the build and lands on top of the container's own `npm ci`.

`docker compose build frontend` is likewise gone — `frontend/Dockerfile` now requires a `UI_PACKAGE`
build-arg with no default. See Frontend (React/Vite) below for the two `docker buildx build` invocations
that replace it.

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

`backend/tests/conftest.py` stubs gRPC modules in `sys.modules` before importing the app so tests run on a dev checkout without needing the protobuf bundle that ships only inside the Docker image. That stub is global, so it would make any in-process check that `master`/`sub`/`botapi` import without `grpcio`/`protobuf`/`docker`/`filelock` pass vacuously — `tests/test_light_role_import_isolation.py` asserts exactly that in a **separate subprocess** instead, where the stub was never installed.

### Frontend (React/Vite)
`frontend/` is an npm workspace of four packages (`packages/ui-core`, `packages/admin`, `packages/node`, `packages/sub-page`) — see Frontend (`frontend/packages/`) below. The root scripts drive all three apps at once; `:admin`/`:node`/`:sub` variants target one:
```bash
cd frontend
npm install
npm run dev            # = dev:admin — admin dev server on :4200 (proxies /api → :5000)
npm run dev:admin      # admin dev server on :4200
npm run dev:node       # node dev server on :4200
npm run dev:sub        # subscription-page dev server on :4300
npm run build          # tsc typecheck + vite build, all three apps
npm run build:admin    # admin only → packages/admin/dist
npm run build:node     # node only → packages/node/dist
npm run build:sub      # subscription page only → packages/sub-page/dist
npm run typecheck      # tsc --noEmit across all four packages
npm run lint           # ESLint
npm run format:check   # Prettier check (CI mode)
npm run format         # Prettier auto-fix
```
There is no root `tsconfig.json` project to typecheck directly — `npx tsc --noEmit` from `frontend/` exits 2 with `TS18002` (the root config is deliberately inert; it exists only so editors resolve paths). Always use `npm run typecheck`.

The two Nginx-served apps build into their own Docker images via the shared `frontend/Dockerfile`'s required `ARG UI_PACKAGE` (no default — an empty value fails the build):
```bash
docker buildx build --build-arg UI_PACKAGE=admin --build-context project=. --tag panel-frontend-admin:local --load ./frontend
docker buildx build --build-arg UI_PACKAGE=node  --build-context project=. --tag panel-frontend-node:local  --load ./frontend
```
`--build-context project=.` (repo root) is required — the Dockerfile reads `versions.json` from it to bake `__APP_VERSIONS__`. A bare `docker buildx build ./frontend` with no `UI_PACKAGE` fails fast on `test -n "$UI_PACKAGE"`. `sub-page` has **no** frontend image of its own: it is built by `backend/Dockerfile.sub` and baked into `panel-sub` at `/app/ui`, which Flask serves (override with `SUB_PAGE_DIST`).

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
cd caddy/caddygen && go test -count=1 ./...   # tests for the routes.yaml → Caddy-JSON generator; -count=1 bypasses the test cache, which does not track the docker-compose.bot.yml / routes.yaml files these tests read from outside the Go module
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
| `backend` (`master`) | Admin API + APScheduler crons (gunicorn + gevent, single worker) — runs the `panel-master` image; no local Xray, no billing surface |
| `backend` (`worker`/node) | Same Flask app plus the local Xray driver — runs the `panel-worker` image, the only one of the four per-role images carrying the Xray binary and the generated protobuf stubs |
| `backend` (`sub`) | Subscription links only — runs the `panel-sub` image, which also serves the React subscription page baked in at `/app/ui` |
| `backend` (`bot-api`) | `/bot-service/*` and the whole billing surface — runs the `panel-bot-api` image |
| `frontend` (`docker-compose.master.yml`) | The admin SPA (full UI, incl. Bot/Panels/Statistics) served by Nginx — runs the `panel-frontend-admin` image |
| `frontend` (`docker-compose.node.yml`) | The node SPA (Dashboard/Routing/System only, no page of its own) served by Nginx — runs the `panel-frontend-node` image |
| `caddy` | Reverse proxy — caddygen-built native JSON, SNI routing on `:443` (caddy-l4), `:80→:443` redirect, TLS from mounted certs, decoy masquerade |
| `redis` | Rate limiting + sub-cache + bot pubsub channel |
| `socket-proxy` | Restricts Docker socket access to specific API ops |
| `bot` | Telegram bot (Aiogram, asyncio) — runs on the master only |

Three networks: `panel-net` (frontend/backend/caddy + xray + bot — the only one with internet egress) plus two `internal: true` segments: `redis-net` (backend ↔ redis ↔ bot) and `dockersock-net` (backend ↔ socket-proxy). The split (formerly a single `control-net`) keeps the Docker-socket proxy reachable only by `backend` and denies internet to both `socket-proxy` and `redis`. Key volumes: `shared_config:/etc/xray`, `xray_logs:/var/log/xray`, `./db_data:/app/db`, `./certs:/root/cert:ro`. Published ports on `caddy`: `80:80`, `443:443` (TCP only — there is no `443/udp` / HTTP-3).

In the split Postgres deployment, `PANEL_ROLE` selects one of four Flask app factories (`panel_core.roles.{master,worker,sub,botapi}`): `master` (default — admin API, no local Xray, and **no billing surface**: it registers neither the `billing` nor the `bot_service` blueprint) runs against Postgres via `DATABASE_URL`; `worker` — called a **node** below — has its own Xray, but (per `docker-compose.node.yml` / `.env.example`) has no `DATABASE_URL` at all, so it runs against its own local SQLite (`./db_data`) as a cache/fallback rather than sharing the master's Postgres; `sub` serves subscription links only; `bot` (bot-api) serves `/bot-service/*` **and the whole billing surface** — `/api/billing/checkout`, the YooKassa webhook, and the three payment crons. A node and a Panel Federation `LinkedPanel` (see Panel Federation below) are two views of the same thing, not separate systems: the node is the process role (`PANEL_ROLE=worker`), while `LinkedPanel` is the row the master's Postgres uses to address it (url + `federation_token`). The master routes provisioning to a node through exactly that federation path — `TariffItem.panel_id` → `LinkedPanel` → `FederationClient.provision()` → `POST /api/federation/provision` on the node (`services/panel_proxy.py`, `api/federation.py`) — which is also *why* a node can't resolve `lang`/`renewable` itself: it has no Postgres access to `TelegramUser`/`Tariff`, only its own local SQLite.

Two Redis instances play different roles once nodes are split out. The `redis` above is per-stack private state (rate limiting + sub-cache, `RATELIMIT_STORAGE_URI`) and never leaves that host. The `bot:events` bus is separate: a data-tier Redis defined in `docker-compose.postgres.yml`, shared by master/bot-api/sub/bot and every node, addressed by `BOT_EVENTS_REDIS_URI` (defaults to `RATELIMIT_STORAGE_URI`). **The default is a trap for any role that has a local Redis.** The rule: a role must set `BOT_EVENTS_REDIS_URI` explicitly whenever its `RATELIMIT_STORAGE_URI` resolves to a *local* Redis — that is **both the master and every node** (`docker-compose.master.yml` and `docker-compose.node.yml` each ship a `redis` container on an `internal: true` network, and both require the variable via `:?`). `sub` and `bot` have no local Redis — their `RATELIMIT_STORAGE_URI` already points at the data tier, so the default is correct there. Getting this wrong is silent and permanent: publishing into an unsubscribed Redis still succeeds, so `delivered_at` is stamped and the replay cron never retries. That data-tier Redis ACLs two users: `node` (publish-only into `bot:events`, plus `select` so a non-zero DB index in the URI still connects) and `panel` (everything except `@dangerous` — no `FLUSHALL`/`CONFIG`/`KEYS`/`SHUTDOWN`/`DEBUG`). See Bot event recovery buffer and Configuration below.

### Backend (`backend/`)

**Where the code actually lives.** The backend is a uv workspace (`backend/pyproject.toml` → `[tool.uv.workspace] members = ["packages/*"]`) with **six** distributions under `packages/`, all of which install files into the *same* namespace package `panel_core` (each one's `[tool.hatch.build.targets.wheel] packages = ["src/panel_core"]`). **Imports do not depend on which distribution a module ships from** — `panel_core.api.billing` and `panel_core.api.inbound` are written identically no matter that they come from different wheels:

| Distribution | Ships | Deps |
|---|---|---|
| `panel-core` | the shared foundation — everything not listed in the other five rows | flask (+sqlalchemy/migrate/cors/limiter/apscheduler), gunicorn, gevent, psycopg2-binary, psycogreen, redis, pyjwt, requests, pyyaml, cryptography |
| `panel-adminapi` | `api/{auth,inbound,outbound,routing,statistics,system,federation}.py` | `panel-core` + **psutil** |
| `panel-worker` | `xray/{local,engine,grpc_client}.py`, `services/stats.py`, `roles/worker.py` | `panel-core`, `panel-adminapi`, `panel-sub` + **docker, filelock, grpcio, grpcio-tools, protobuf** |
| `panel-master` | `api/{bot_admin,panels}.py`, `jobs/{billing,panels}.py`, `roles/master.py` | `panel-core`, `panel-adminapi`, `panel-sub` |
| `panel-sub` | `api/subscription.py`, `roles/sub.py` | `panel-core` |
| `panel-botapi` | `api/{billing,bot_service}.py`, `services/billing.py`, `jobs/payments.py`, `roles/botapi.py` | `panel-core` + **`yookassa>=3.0,<4.0`** |

**`yookassa` is a dependency of `panel-botapi` only** — it is not in `panel-core`'s dependency list, and `uv sync --package panel-core` does not install it. Importing `panel_core.roles.master` leaves `yookassa` out of `sys.modules`; only `panel_core.roles.botapi` pulls it in. Keep it that way: never import `yookassa` (or `panel_core.services.billing`) from a `panel-core` module.

**`panel-sub` used to be a dependency inversion for the master and worker — that is now history.** Through Phase 3c-3, `roles/master.py` and `roles/worker.py` still shipped from `panel-core` while registering the `subscription` blueprint, which ships from `panel-sub`; `panel-sub` declares `dependencies = ["panel-core"]`, so that reverse edge could not be declared without a workspace cycle, and `uv sync --package panel-core` alone could not build either role (`ImportError: cannot import name 'subscription' from 'panel_core.api' (unknown location)`). The `panel-master`/`panel-worker` cut resolved it: `roles/master.py` now ships from `panel-master` and `roles/worker.py` from `panel-worker`, and both declare `panel-sub` (and `panel-adminapi`) as ordinary dependencies. **`uv sync --package panel-core` now yields a buildable core on its own, and `ALLOWED_INVERSIONS` is empty** — the recorded exit criterion of the cut has been met. `dispatch.py` still ships from `panel-core` and still imports `roles/{sub,botapi,master,worker}` to dispatch to them; that edge lives in the separate, permanent `ROLE_DISPATCH_EXEMPTIONS` set (now four entries, one per role), not in `ALLOWED_INVERSIONS`. `panel-worker` and `panel-botapi` are the two distributions genuinely absent from the master's import graph.

**Import direction between distributions is guarded** (`tests/test_distribution_imports.py`). Because `panel_core` is one namespace package, an import statement says nothing about which wheel the target ships from — `from panel_core.services.billing import apply_payment` inside a `panel-core` module reads like a local import while actually inverting the dependency graph and pulling the `yookassa` SDK into every image. The guard resolves each `panel_core.*` import to its owning distribution and requires that owner to be inside the importer's **declared** dependency closure, read from the `pyproject.toml` files rather than hardcoded. The `yookassa` guard in `tests/test_workspace_layout.py` does **not** cover this: it matches literal `import yookassa` statements and never follows a `panel_core.*` edge. Two exemption sets exist, each with its own rationale in the file: the now-empty `ALLOWED_INVERSIONS` above, and `ROLE_DISPATCH_EXEMPTIONS` — `dispatch.py`'s `PANEL_ROLE` branches import `roles/{sub,botapi,master,worker}` *inside* `create_app()`, so each edge is only traversed on a host that installs that distribution by definition. That one is structural and permanent, and holds only while those imports stay function-level (separately asserted).

Every `app/…` path in the list below is shorthand for `backend/packages/<dist>/src/panel_core/…` — e.g. `app/models.py` is `packages/panel-core/src/panel_core/models.py`, imported as `panel_core.models`; `app/api/billing.py` is `packages/panel-botapi/src/panel_core/api/billing.py`, imported as `panel_core.api.billing`.

**`panel_core` is a namespace package (PEP 420).** Neither it nor its splittable subpackages (`api/`, `services/`, `jobs/`, `roles/`, `xray/`, `data/`) carries an `__init__.py`, which is what lets the six distributions above ship into the same import root. This is no longer hypothetical: `panel_core.__path__` has **six** contributions today, and every cut from the original three to today's six changed **zero** call-site import statements outside the moved modules themselves. Consequences you must not undo (guarded by `tests/test_namespace_packages.py`, `tests/test_workspace_layout.py`, `tests/test_xray_facade.py`, `tests/test_bootstrap.py` — the workspace guard also fails on a module shipped by two distributions at once, and on a workspace member with Python code that no guard scans):
- **Importing `panel_core` runs no code.** What the deleted `__init__.py` files held now lives in explicit modules: `bootstrap.py` (`bootstrap_gevent()` — `gevent.monkey.patch_all()` + `patch_gevent_psycopg()`), `dispatch.py` (`create_app()`, the `PANEL_ROLE` → role-module dispatcher) and `xray/facade.py` (the gateway shims `has_local_xray`, `generate_config_file`, `restart_xray_container`, `stream_xray_logs`, `update_geo_db`, `_api_add_user_grpc`, `_api_remove_user_grpc`). Import them from those modules. Both of the old forms are **already broken today**, not merely fragile under a future split: `from panel_core.xray import generate_config_file` raises `ImportError: cannot import name 'generate_config_file' from 'panel_core.xray' (unknown location)` and `from panel_core import xray` + `xray.generate_config_file` raises `AttributeError`, because a namespace package owns no `__init__.py` and so re-exports nothing. The guard (`tests/test_xray_facade.py`) exists to stop either form being re-introduced — it is not a pre-emptive check against a split that has not happened yet. `xray/facade.py` ships from `panel-core` and dispatches to whichever gateway is bound at runtime; the local implementation it shims, `LocalXrayGateway`, lives in `xray/local.py` and ships from `panel-worker` — the only role with a local Xray to gate.
- **gevent patching is now every entry point's own job.** `run.py` (dev) calls `bootstrap_gevent()` on its first lines; `tests/conftest.py` calls it before importing anything else from `panel_core`. In containers nothing in Python does it — gunicorn's own worker does: `GeventWorker.init_process()` calls `gevent.monkey.patch_all()` before `base.Worker.init_process()` reaches `load_wsgi()`. That holds only while the gunicorn command keeps `-k gevent` and stays **without `--preload`** (with `--preload` the arbiter imports the app in the unpatched master process before forking). `tests/test_compose_gunicorn_gevent.py` guards both conditions across all seven gunicorn commands in `docker-compose*.yml`.
- **psycopg is patched on every *role* path** regardless: `build_base_app()` calls `patch_gevent_psycopg()` itself, so all four roles get the gevent wait callback even though `bootstrap_gevent()` was never called in-process (`tests/test_bootstrap.py` parametrises that over all four). The one exception is `sqlite_to_pg.py`, which builds no Flask app and calls neither `bootstrap_gevent()` nor `patch_gevent_psycopg()` — it reaches Postgres as plain blocking psycopg2. That is the right mode for a one-shot CLI migration (there is no gevent hub to block), but it *is* a behaviour change the namespace conversion made: the script used to inherit the patch from the deleted `panel_core/__init__.py`, and nothing replaced that side effect. Do not describe the patch as universal.
- **Package data is reached through `panel_core/resources.py`, never through `__file__`.** `resources.data_file(name)` / `read_data_text(name)` resolve via `importlib.resources.files("panel_core.data")`, which on 3.12 returns a `MultiplexedPath` that searches *every* distribution contributing to the namespace. The `__file__`-relative form is the same defect class as the `instance_path` one and fails the same way: `api/bot_admin.py` did `os.path.join(os.path.dirname(__file__), "..", "data")`, which under a two-distribution **editable** install (production's mode — `uv sync --frozen --no-dev`) resolves into the *api* distribution's tree, where `data/` does not exist. It failed silently — `GET /api/bot/texts/keys` returned HTTP 200 with `{"keys": []}` and the Bot → Texts tab went blank, no error, no log line. `db_migration.py`'s bot-texts seeder had the same shape (`__file__` + `"data"`). A non-editable wheel merges both trees into one `site-packages/panel_core/` and hides all of it, so this only ever breaks in production's install mode. `tests/test_resource_paths.py` rejects any `__file__`-derived path segment naming `..` or a namespace subpackage.
- **`root_path` and `instance_path` are passed to `Flask` explicitly** (`app_base.py`: `Flask("panel_core", root_path=PACKAGE_ROOT, instance_path=INSTANCE_PATH)`). Flask derives `root_path` from the package's `__file__` (a namespace package has none) and `instance_path` via `_find_package_path`, whose namespace branch does a bare `next()` over the search locations and raises `StopIteration` as soon as more than one location contributes — so leaving either to auto-discovery would break the moment the package is actually split. `INSTANCE_PATH` is `sys.prefix/var/panel_core-instance`: `sys.prefix` is unambiguous no matter how many distributions contribute, while any formula derived from the package location is not. Production installs `panel-core` **editable** (`uv sync --frozen --no-dev` in `backend/Dockerfile`), so this changed the value from `/app/packages/panel-core/src/instance` — harmless, because nothing reads `instance_path`. The only way to make it meaningful is a *relative* sqlite `DATABASE_URL` (`sqlite:///panel.db`), which Flask-SQLAlchemy resolves against `app.instance_path`. Nothing reaches that path today. Three of the eight compose files set `DATABASE_URL` at all — `docker-compose.{master,sub,bot}.yml`, each as a pass-through `${DATABASE_URL:?…}` that the compose file itself does not constrain, and `.env.example` fills all three with a `postgresql+psycopg2://…` URI. `docker-compose.node.yml` deliberately sets none (see the role paragraph above): the worker falls through `db_config.database_uri()` to `sqlite:///` + `app_base.db_path()`, which is **absolute** (`$CWD/db/panel.db`, mounted from `./db_data`) and therefore never consults `instance_path`. So a relative sqlite URI would have to be set by hand, against the only three roles whose compose requires the variable and expects Postgres — it is reachable, but nothing in the repo produces it.

- `app/app_base.py` + `app/dispatch.py` + `app/roles/{master,worker,sub,botapi}.py` — Flask app factories; register blueprints, extensions, ProxyFix, APScheduler jobs per role
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
  - `traffic_store.py` — pure SQL layer for traffic storage, usable by roles with **no local Xray**: snapshot upserts (`_ten_min_bucket`, `_upsert_snapshot`, `_upsert_node_snapshot`, `_upsert_domain_stat`), cleanup (`cleanup_old_domain_stats`, `cleanup_stats_job`), and the admin-surface counter resets (`reset_user_traffic`, `reset_inbound_traffic`, `bulk_delete_users`) that touch Xray only through `XrayGateway`
  - `stats.py` — worker-side traffic collector: gRPC polling (`sync_traffic_stats`), limit enforcement + monthly reset (`check_limits_and_reset`), access-log parsing; re-exports the names above from `traffic_store` for backward-compatible imports and test patches. `check_limits_and_reset` and `sync_traffic_stats` also emit `expiry_notification`/`traffic_notification` events inline (see `notifications.py` below and Bot event recovery buffer)
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

### Frontend (`frontend/packages/`)

`frontend` is an npm workspace (`frontend/package.json`: `workspaces: ["packages/*"]`) of four packages, each with its own `package.json`, `vite.config.ts`, `tsconfig.json`, `index.html`, `tailwind.config.js` and `postcss.config.js` — there is no root-level `index.html`/`vite.config.ts`/`tailwind.config.js`/`postcss.config.js` any more; only `entrypoint.sh` and `nginx.conf.template` stay shared at `frontend/`. Neither `@panel/admin`'s nor `@panel/node`'s nor `@panel/sub-page`'s `package.json` declares a dependency on `@panel/ui-core` — there is no workspace dependency edge at all, only an alias: each package's `tsconfig.json` and `vite.config.ts` map `@ui` → `../ui-core/src` and `@` → the app's own `src` (so a bare `@/pages/Panels` inside `admin` can never resolve inside `ui-core`, and vice versa). That alias is necessary but not sufficient — a relative specifier crosses the same boundary without ever touching it, which is why the import direction is also enforced by a dedicated guard (`backend/tests/test_frontend_import_direction.py`) rather than by the alias or the dependency graph.

- `packages/ui-core/src/` — everything shared by the three apps (55 files, every file under the directory regardless of extension — 33 `.ts`/`.tsx`, `index.css`, plus `fonts.css` and the 20 self-hosted Roboto/Roboto Mono `.woff2` files added in Phase 6 when the Google Fonts CDN link was dropped): `pages/` (`Dashboard`, `Routing`, `System`, `Login` — the four pages every role has), `components/inbound/` (`InboundForm`, `UserForm`), `components/ui/` (`Select`, `Modal`, `ConfirmationModal`, `Button`, `Input`, `Switch`, `TagInput`), `components/layout/` (`Layout`, `Sidebar`, `AnimatedBackground`), `components/DisplayConfigLoader.tsx`, `hooks/` (`useLinkedPanels`, `useVersionStatus`), `lib/` (`api.ts` — axios client with auth interceptor; `types.ts` — TS interfaces for every API entity; `protocols.ts` — protocol + stream-settings definitions; `panelRole.ts`/`assertPanelRole.ts` — role gating, see the deploy note below; `panelBase.ts`, `datetime.ts`, `devices.ts`, `routing-validation.ts`, `utils.ts`, `version.ts`), `stores/` (Zustand stores for auth + log state), `index.css`.
- `packages/admin/src/` — admin-only surface (19 files, same counting rule as ui-core above — this one happens to be all `.ts`/`.tsx`): `App.tsx`, `main.tsx` (the entry points), and the master-only pages/components `pages/Statistics.tsx`, `pages/Panels.tsx` (federation management), `pages/Bot.tsx` (billing UI) plus `components/bot/` (`TariffsTab`, `TariffDrawer`, `TariffsTable`, `TariffRowMenu`, `UsersTab`, `UserDrawer`, `GrantsTab`, `PaymentsTab`, `PaymentStatusBadge`, `TextsTab`, `SettingsTab`, `TrialCard`) and `lib/bot.ts`.
- `packages/node/src/` — **has no page of its own**: just `App.tsx`, `main.tsx`, `vite-env.d.ts` (3 files, same counting rule as the two bullets above). `App.tsx` wires up only the shared `Dashboard`/`Routing`/`System`/`Login` pages from `ui-core` (`Routing` further gated by `hasLocalXray`, which is always true for this image) — every route with its own page component lives in `ui-core` or `admin`, never in `node`.
- `packages/sub-page/src/` — the subscription page a user opens in a browser, and the only package that is **not** an admin surface (18 files, same counting rule as the bullets above): `App.tsx`, `main.tsx`, `vite-env.d.ts`, `components/` (`Header`, `Hero`, `Summary`, `QrPanel`, `AppButtons`, `Nodes`, `Footer`, `Loading`, `ErrorState`), `hooks/useSubInfo.ts`, `lib/` (`deeplinks.ts`, `format.ts`, `i18n.ts`, `types.ts`), `index.css`. It has no router, no axios client and no auth store — it reads one endpoint, `GET /api/sub/u/<token>/info`. Three things set it apart from the two admin apps: it ships **no** `assertPanelRole()` call and reads no `panel-role` meta tag (it is served by Flask out of `panel-sub`, not by Nginx, and there is no role to get wrong); it carries **its own `index.css`** rather than importing `ui-core`'s, because that one applies `overflow-hidden` to `body` for the fixed-chrome admin layout and would make a scrolling page unreadable on a phone; and it is built into the `panel-sub` backend image by `backend/Dockerfile.sub` rather than into an Nginx image of its own. It still looks like the same product, but only one of the two reasons is actual sharing: `ui-core/src/fonts.css` (self-hosted Roboto) is imported by all three packages and is sub-page's **only** edge into `ui-core`, while the Tailwind theme is a *duplicated copy* — `ui-core` has no `tailwind.config.js`, each package declares its own palette, and sub-page's config does not even scan `../ui-core/src`. That distinction decides the release fan-out — see point 3 of the Phase 3d deploy note.

Each of the two admin apps bakes its role at build time (`vite.config.ts`'s `define: { __EXPECTED_PANEL_ROLE__ }`) and asserts it at runtime against the `<meta name="panel-role">` tag that `entrypoint.sh` rewrites in `index.html` at container start (read by `lib/panelRole.ts`'s `readInjectedPanelRole()`). A meta tag, not an inline script: the reverse proxy's CSP sets `script-src 'self'`, which blocks inline `<script>` outright — see the deploy note below.

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

Caddy loads **one** cert pair from `/root/cert/{fullchain,key}.pem` (mounted from `./certs`) for **all** terminated SNIs — a multi-domain deploy therefore needs a single **SAN** cert covering panel + sub. Issue/renew with `scripts/generate_certs.sh`: it stops Caddy (to free `:80`, which Caddy otherwise holds via the published port), runs `certbot certonly --standalone --expand` for `PANEL_DOMAIN` (+ `SUB_DOMAIN`), copies `fullchain.pem`/`privkey.pem` into `./certs`, and brings Caddy back (trap, even on failure). **Renewal is the same command, run manually** — there is no cron, and certbot's own timer can't bind `:80` while Caddy runs (and wouldn't propagate into `./certs` anyway). **The script only works on the host that actually serves `PANEL_DOMAIN`, running the default compose project**: `-d "$PANEL_DOMAIN"` is unconditional (certbot is all-or-nothing, so a `PANEL_DOMAIN` resolving to some *other* box fails the entire run), and its `docker compose stop/up caddy` calls carry no `-f`, so they resolve `docker-compose.yml`. The standalone **sub** and **bot** hosts therefore issue their own single-domain certs by hand — see the bot-host recipe in the deploy note below; `BOT_DOMAIN` is deliberately **not** in this script's domain list. `scripts/generate_local_cert.sh` writes a self-signed cert for local domains. Both installers (`scripts/install_{dev,prod}.sh`) run a cert step **before** bringing Caddy up — Caddy won't start without `./certs/fullchain.pem`.

### Traffic enforcement
`stats.py` polls per-user up/down via Xray gRPC every 10s, writes to `Client.up`/`down` and upserts hourly `TrafficSnapshot` rows. `check_limits` (60s) removes users that exceed limit or expiry. Monthly resets (per-client `reset_day`) zero the counters **and** delete that client's `traffic_*` `NotificationLog` rows so the next cycle's warnings can fire.

### Background scheduler jobs

| Job | Interval | What it does |
|---|---|---|
| `sync_traffic` | 10s | Per-user up/down from Xray gRPC; upserts `TrafficSnapshot` via raw SQL `ON CONFLICT DO UPDATE`; emits `traffic_notification` inline at 80%/95%/exhausted (dedup via `NotificationLog`) |
| `check_limits` | 60s | Removes expired/over-limit users; emits `expiry_notification` inline at 3d/1d/1h/expired (dedup via `NotificationLog`) |
| `parse_logs` | 15s | Tails Xray access logs into `DomainStat` (skips bare IPs) |
| `cleanup_stats` | 24h | Runs on **master and worker** roles; deletes `DomainStat` rows > 90d |
| `poll_linked_panels` | 10s | Pings each enabled `LinkedPanel`; fresh `status`/`last_poll` go to Redis every poll, the SQLite row is written **only on status/error change** (the panels API overlays the Redis values) |
| `auto_renew_free_users` | 15m | Re-provisions due `billing='free'` grants; pauses + emits `access_paused` on tariff archive/disable (does **not** force-disable clients — they lapse via their own `expiry_time`) |
| `poll_pending_payments` | 30s | Runs on the **`bot` (bot-api) role only** — not the master; webhook fallback, reconciles pending YooKassa payments older than 30s, younger than 24h |
| `reconcile_refunds` | 1h | Runs on the **`bot` role only**; refund-webhook fallback — re-checks the most recent succeeded payments (≤30d, capped 200) and revokes access on any YooKassa now reports refunded (via `billing.handle_refund`) |
| `cleanup_old_payments` | 24h | Runs on the **`bot` role only**; cancels `pending > 24h` (and publishes `payment_cancelled` so users find out); deletes terminal records `> 90d` |
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
Protocol details live in `frontend/packages/ui-core/src/lib/protocols.ts` (UI-facing) and are serialized to JSON in backend models. Client IDs must be valid UUIDs for VLESS/VMess/Trojan, valid WireGuard private keys for WireGuard. Shadowsocks 2022 server/user passwords must be base64-encoded keys of the correct byte length (16 bytes for AES-128, 32 bytes for AES-256 and ChaCha20).

### Subscription links
`api/subscription.py` serves `GET /api/sub/<uuid_str>` — UUID-keyed, so renaming `Client.email` does NOT break a user's existing app config. The response can merge entries from linked panels visible to the user. Cached in Redis with a configurable TTL (`subscription_update_interval_hours` SystemSetting). `build_aggregate_sub_url(token)` builds the link the bot/dashboard show: it **prefers `SUB_DOMAIN`** (`https://<SUB_DOMAIN>/api/sub/u/<token>`) and falls back to `PANEL_DOMAIN` + `PANEL_SECRET_PATH` when `SUB_DOMAIN` is empty. The env var must be present on the **backend** container for this to take effect.

That same `/api/sub/u/<token>` URL serves two audiences off one route: a client app's User-Agent gets the raw config, a browser gets the **React subscription page** — `frontend/packages/sub-page`, built by `backend/Dockerfile.sub` and baked into `panel-sub` at `/app/ui` (override with `SUB_PAGE_DIST`), with its assets under `/api/sub/u/assets/…`. The page is a static bundle and holds no data of its own; it fetches `GET /api/sub/u/<token>/info` for the JSON it renders (traffic used/limit, expiry, per-node entries, deep-links). A missing bundle 503s the page and its assets **without** touching config delivery — the sub role's critical function stays alive. The server-rendered HTML page this replaced is gone; there is no `<!doctype html>` left in the Python.

**Only the `sub` role serves the subscription page.** `panel-sub` is the one image that bakes a bundle, but the `subscription` blueprint it ships is also registered by `roles/master.py` and `roles/worker.py` — so those two roles expose the same routes with no bundle behind them, and their browser branch answers 503 while their client-app branch returns raw configs exactly as before. That is what makes **`SUB_DOMAIN` required for the page to exist**: leave it empty and `build_aggregate_sub_url` falls back to `PANEL_DOMAIN` + `PANEL_SECRET_PATH`, which `caddy/routes.yaml` sends straight to the master, so the link handed to users renders nothing in a browser. Client apps are unaffected in either topology — the User-Agent fork gives them the config, which is why this fails quietly. Baking the bundle into `master`/`worker` as well was considered and rejected: three images would carry a Node build stage to serve a page two of them have no business serving.

### Custom Select component
`frontend/packages/ui-core/src/components/ui/Select.tsx` renders a portal-based dropdown instead of a native `<select>`. It synthesizes a `React.ChangeEvent<HTMLSelectElement>` in its `onChange`. When used with react-hook-form, always spread `{...register('fieldName')}` so the `name` prop is passed — react-hook-form looks up the field by `event.target.name` and silently ignores the change if `name` is missing or empty.

### Default outbounds
On startup, `direct` (freedom) and `block` (blackhole) outbounds are auto-created if missing. These are always re-enabled if disabled — do not delete them.

### Database migrations
`panel_core.db_migration` (standalone entrypoint: `backend/migrate_db.py`) is a custom migration system (not Flask-Migrate). Current schema version is **`23`**, tracked via `PRAGMA user_version`. The script is idempotent — runs on every backend startup, uses `CREATE TABLE IF NOT EXISTS` for new tables and `ALTER TABLE ADD COLUMN` (with `_add_column_if_missing` guard) for column additions. All `ALTER`s are SQLite metadata-only (O(1)), so migration time is independent of row count. When adding a new table: add a `_ensure_<name>_table` function, call it from `migrate_sqlite_db`, bump `CURRENT_DB_VERSION`.

Bot texts have their own version: `CURRENT_BOT_TEXTS_VERSION = 17`. A bump triggers a one-shot **force-reseed** (only when `stored < CURRENT`): it DELETEs the `_REMOVED_BOT_TEXT_KEYS` tuple (purging orphan rows for keys dropped from the YAML) and then upserts every `(key, lang)` pair from `app/data/bot_texts_defaults.yaml` (~74 keys × RU/EN). The upsert **preserves admin-edited rows** — `bot_text.customized` (set to `1` whenever an admin saves a text via Bot → Texts) is honoured by `ON CONFLICT … DO UPDATE … WHERE customized = 0`, so a force-reseed refreshes only untouched defaults and never reverts customizations. On the v19 migration that added the column, rows whose stored text already diverged from the YAML default are back-filled `customized=1` to protect pre-existing edits. When you remove a key from the YAML, append it to `_REMOVED_BOT_TEXT_KEYS` (the purge ignores `customized`, since a removed key is dead regardless).

> **Reseed gotcha:** the purge/overwrite only fires when `stored < CURRENT`. An install already **at** the current number but with older content (e.g. a dev box that ran an unreleased build at the same version) is skipped — new keys still appear via the non-force `INSERT OR IGNORE` seed, but removed/changed keys don't. Coming from a real release baseline it's always clean; to force a clean reseed on such a dev box, set `system_setting.bot_texts_seeded_version` below CURRENT and restart the backend. To guarantee a reseed on *every* install regardless of prior unreleased numbers, bump strictly above the highest number any box has stored.

### Python dependencies & Docker images (uv)
Both Python services (`backend/`, `tg_bot/`) are **uv projects**: dependencies live in `[project].dependencies` in each `pyproject.toml`, pinned by a committed `uv.lock` (reproducible builds — previously every rebuild floated to latest). `[tool.uv] package = false` marks them as applications (install deps only, no wheel build), and `requires-python = "==3.12.*"` matches the `python:3.12-slim` base and the `grpcio==1.66.2` pin. There is **no `requirements.txt`** — `uv sync` is the install path everywhere.

Dockerfiles are **multi-stage**: a builder stage runs `uv sync --frozen --no-dev` into `/app/.venv`, then the final stage copies only `/app/.venv` + code — no `uv` binary, no `git`, no `build-essential` in the runtime image. The backend now builds as **four** per-role images from **three** Dockerfiles instead of one monolithic `backend` image: `backend/Dockerfile` takes a required `ARG PANEL_PACKAGE` (no default — an empty value fails the build via `test -n "$PANEL_PACKAGE"`) and is reused for `panel-master` (**211 MB**, 33.4% smaller than the monolith) and `panel-bot-api` (**222 MB**, 29.9% smaller) by passing `--build-arg PANEL_PACKAGE=panel-{master,botapi}`; `backend/Dockerfile.worker` is a **separate file**, not another `PANEL_PACKAGE` value, because only the worker needs the Xray binary (`COPY --from=xraybin`) and the `grpc_tools.protoc`-generated protobuf stubs (`XRAY_CORE_REF`-pinned), which would otherwise bloat the light images — it hardcodes `--package panel-worker` and produces `panel-worker` (**311 MB**, still under the monolith but only 1.8% smaller, since it alone keeps the Xray runtime); `backend/Dockerfile.sub` is the third, and likewise takes no `PANEL_PACKAGE` (it hardcodes `--package panel-sub`), because it alone carries a `node:20-alpine` stage that builds `@panel/sub-page` and bakes the bundle into `/app/ui` — `panel-sub` measures **210.3 MB**, up 437 KB from the **209.9 MB** it was when it still built off the shared Dockerfile, which is the bundle and nothing else (the Node stage never reaches the runtime image). The old monolithic `backend` image was **316.8 MB**; state the four measured sizes rather than a percentage range when reasoning about this, since the range drifts every time one image changes independently of the others. The `uv` binary comes from the pinned `ghcr.io/astral-sh/uv:0.11.19` image; `UV_LINK_MODE=copy` keeps the venv relocatable across stages, `/app/.venv/bin` is first on `PATH`, and a `.dockerignore` keeps the local `.venv`, `secret.key`, `tests/` and `db/` out of the build context. `UV_PYTHON_DOWNLOADS=0` forces uv to use the base image's interpreter. All three Dockerfiles insert a dependency-only cache layer between the `pyproject.toml` COPYs and `COPY packages/`, so an app-code change doesn't invalidate the dependency-install layer — but they sync a different package there: `backend/Dockerfile` syncs `--package "$PANEL_PACKAGE"` (whichever role is being built), while `backend/Dockerfile.worker` and `backend/Dockerfile.sub` hardcode `--package panel-worker` / `--package panel-sub` in that cache layer too, the same way their main sync steps do. There is also a **repo-root `.dockerignore`** now, which every `--build-context project=.` draws through: the other Dockerfiles take only `versions.json` from that context and never noticed it was unfiltered, but `Dockerfile.sub` copies all of `frontend/` out of it, and the host's `frontend/node_modules` would otherwise be shipped in (212 MB) and land on top of the `npm ci` the ui stage just ran. The bot image is unaffected by this split (~176 MB).

CI installs uv via `astral-sh/setup-uv@v8.2.0` and runs `uvx ruff` for lint, `uv sync --frozen` + `uv run pytest` for tests. `uv sync` installs the dev group (`pytest`, `pytest-flask`) — note `pytest-flask`'s autouse fixtures pull the `app` fixture ahead of other autouse fixtures, so test mocks must patch a name **where it is used** (`app.api.inbound.restart_xray_container`), not only where it is defined (`app.services.xray.*`); the source-module patch silently misses because `api/inbound.py` did `from app.services.xray import …`.

### Statistics storage
`TrafficSnapshot` stores hourly traffic deltas per entity (user or inbound) **forever** — space is ~100 bytes × entities × 8760 hours/year, negligible for typical deployments. `DomainStat` stores daily domain hit counts and is pruned to 90 days. Both use SQLite `ON CONFLICT DO UPDATE` upserts via `literal_column()` + raw `text()` SQL — do not replace with ORM insert, it breaks atomicity.

### Secret path injection
The frontend is served under `PANEL_SECRET_PATH`. At container startup, `frontend/entrypoint.sh` rewrites two things in the built `index.html` with `sed` — the `<base href="/">` becomes `<base href="/<secret>/">`, and `<meta name="panel-role" content="__PANEL_ROLE__">` gets the container's validated `PANEL_ROLE` — then generates `nginx.conf` from `nginx.conf.template` (which proxies `/<secret>/api/` to `backend:5000`). All traffic outside the secret path returns 404. There is **no** `window.__PANEL_BASE_URL__` and no injected inline script anywhere in that path: the CSP the reverse proxy sets uses `script-src 'self'`, so an inline `<script>` would simply not execute, and both facts now travel as HTML attributes that survive it.

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
| TypeScript typecheck | `cd frontend && npm run typecheck` |
| ESLint | `cd frontend && npm run lint` |
| Prettier | `cd frontend && npm run format:check` |
| Frontend build | `cd frontend && npm run build` |
| Backend pytest | `cd backend && uv sync --frozen && uv run pytest tests/ -q` |
| Bot pytest | `cd tg_bot && uv sync --frozen && uv run pytest tests/ -q` |
| Dockerfile lint | hadolint (runs in CI only) |

CI provisions uv via `astral-sh/setup-uv@v8.2.0` (there is no moving `v8` major tag — pin the exact version), then runs the commands above through `uvx` / `uv run`.

`uvx ruff format <dir>` and `npm run format` auto-fix formatting issues — run them before committing, not after CI fails. The `caddygen` Go tests (`cd caddy/caddygen && go test -count=1 ./...`) are not in CI but should pass after caddygen changes; `-count=1` is required — plain `go test ./...` can print a stale `ok (cached)` because `compose_test.go` reads `docker-compose.bot.yml` and `caddy/routes.yaml` from outside the Go module, which the test cache does not track. markdownlint is **not** run in CI.

CI **runs pytest** — the `Backend pytest` job (`ci.yml`) and the `Bot pytest` job both run `uv run pytest tests/ -q` after `uv sync --frozen` — so a test failure in either suite turns CI red and blocks `main`. Run the suite locally and confirm it's green before pushing; add tests when behavior changes — see `backend/tests/` for patterns. Watch for date-dependent tests: seed timestamps relative to the current month/day can flip near month/day boundaries.

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
4. CI diffs the new `versions.json` against the previous commit and builds/pushes **only the services whose version string changed**. If only `xray_core_ref` changed it's a no-op; bump `worker` too to force a rebuild — it's the only image the Xray core ref affects.
5. CI does **not** commit anything back to `main`. There is no auto-bump commit.

Force-pushing rewrites history — CI can't diff against the old SHA and falls back to `HEAD~1..HEAD`. Avoid force-pushing `main`; use feature branches.

### Panel Federation deploy ordering
When the schema bumps (any `CURRENT_DB_VERSION` change), **deploy master and all linked panels in the same wave**. A master on a newer schema may push user/tariff structures that an older linked panel can't parse. Backup first (`GET /api/backup`), then `docker compose pull && up -d` everywhere.

### Deploy note — the payment surface moved to bot-api (Phase 3c-2)

This wave moves the entire billing surface off the master. Read all six points before rolling it out.

1. **The YooKassa webhook is no longer served by the master.** `/api/billing/yookassa/webhook` now exists **only** on the bot host — `roles/master.py` registers neither the `billing` nor the `bot_service` blueprint; `roles/botapi.py` registers both. **Repoint your YooKassa merchant dashboard's webhook URL to `https://<BOT_DOMAIN>/api/billing/yookassa/webhook` before the rollout**, or confirmations stop landing on the master's now-dead endpoint.

2. **The webhook is reachable again (Phase 3c-2a closed the gap).** The bot host runs its own Caddy, same pattern as the sub host: `docker-compose.bot.yml`'s `caddy` service publishes `80:80`/`443:443` and requires `BOT_DOMAIN` (`:?BOT_DOMAIN is required` — **the bot stack refuses to come up without it, by design**); `caddy/routes.yaml` carries a `bot` SNI route matching `${BOT_DOMAIN}` → `backend:5000` (the `bot-api` container aliases itself as `backend` on `bot-net`), terminates TLS, and allowlists only `/api/billing/yookassa/webhook` via `only_paths`.

   **Narrowing the route is not enough — the *host* has to be narrow too, and that is a property of the compose file, not of `routes.yaml`.** caddygen drops an SNI route only when its `${VAR}` interpolates to the **empty string** (`caddy/caddygen/config.go`); it has no notion of a host role, so every domain variable the Caddy container can see turns another route on. `${PANEL_DOMAIN:-}` does **not** pass an empty string — compose's `:-` substitutes the value from `.env` whenever the variable is *present* and defaults only when it is *absent*, and `PANEL_DOMAIN` is mandatory on a bot host (bot-api needs it for `sub_links.build_aggregate_sub_url` and `federation._build_panel_url`). `env_file: - .env` re-injects them all regardless of the `environment:` block. So `docker-compose.bot.yml`'s `caddy` service carries **`BOT_DOMAIN` and nothing else, and no `env_file`** — the rendered container holds exactly one variable. Because SNI is client-chosen and the box serves its cert for whatever name is asked, a stray `PANEL_DOMAIN` there would make `https://<PANEL_DOMAIN>/<PANEL_SECRET_PATH>/api/…` aimed at the **bot** box's IP land on bot-api, reaching `/api/billing/checkout` and all of `/bot-service/*` — the exact surface `only_paths` exists to withhold (both are token-protected, so this is defence in depth, not an open door). Two guards hold the line: `backend/tests/test_compose_bot_ingress.py` (runs in CI) asserts the bot host's Caddy selects no route but `bot`, and `caddy/caddygen/compose_test.go` renders `docker-compose.bot.yml`'s Caddy environment through caddygen and asserts exactly one layer4 route plus `{bot_security_layer, http_redirect}`.

   **The bot host needs its own certificate covering `BOT_DOMAIN`, issued manually on the box** — it is not distributed from the master, and **`scripts/generate_certs.sh` cannot do it** (same as the sub host). The script is written for the host that serves `PANEL_DOMAIN`: it passes `-d "$PANEL_DOMAIN"` unconditionally, whose DNS points at the master, so the `certbot --standalone` HTTP-01 challenge for it is answered by the master's Caddy and certbot — being all-or-nothing — fails the whole run; and its `docker compose stop caddy` / `up -d caddy` carry no `-f`, so on a bot box they would resolve the legacy monolithic `docker-compose.yml` (see point 6) and replace `panel-bot-caddy`. On the bot box, do it by hand instead:

   ```bash
   set -a; . ./.env; set +a
   docker compose -f docker-compose.bot.yml stop caddy
   certbot certonly --standalone --non-interactive --agree-tos \
       --register-unsafely-without-email --cert-name "$BOT_DOMAIN" -d "$BOT_DOMAIN"
   cp -L "/etc/letsencrypt/live/$BOT_DOMAIN/fullchain.pem" ./certs/fullchain.pem
   cp -L "/etc/letsencrypt/live/$BOT_DOMAIN/privkey.pem"   ./certs/key.pem
   docker compose -f docker-compose.bot.yml up -d caddy
   ```

   Renewal is the same command, run manually — there is no cron, and certbot's own timer can't bind `:80` while Caddy holds the published port. Issue the cert **before** the first `up`: Caddy will not start without `./certs/fullchain.pem` and crash-loops until it exists. With the webhook live, YooKassa confirmations arrive over HTTP again; `poll_pending_payments` (30s) is a fallback for the rare miss, not the primary path. **For as long as the bot host is down, no payment is confirmed at all** — there is no other host that can receive the webhook or run the poll.

3. **Payments are polled by bot-api, not the master.** All three payment crons (`poll_pending_payments` 30s, `reconcile_refunds` 1h, `cleanup_old_payments` 24h) are registered in `roles/botapi.py` and nowhere else. While the bot box is down there is **no** payment reconciliation at all — the master used to be the safety net and no longer is. Combined with point 2, the bot host is a single point of failure for confirming payments. Two consequences of that concentration:
   - **`cleanup_old_payments` asks YooKassa before cancelling.** It re-checks each `pending > 24h` row via `billing.fetch_remote_status`: `succeeded` → `apply_payment` (the late confirmation the poll's 24h bound can no longer deliver), `waiting_for_capture` → left pending, YooKassa unreachable → left pending and retried next run, anything else → cancelled + `payment_cancelled`. It must never cancel on local state alone; that is exactly how a >24h outage would convert genuinely paid payments into "cancelled" on restart. Guarded by `tests/test_payments_jobs.py`.
   - **Refunds have no webhook path either.** `reconcile_refunds` is the *only* mechanism that revokes access after a refund, and it is a sampling job, not a queue: every hour it re-checks succeeded payments from the last **30 days**, capped at the **200** most recent (it logs when it hits the cap, and the older tail simply goes unchecked that run). A refund on a payment outside that window is never noticed.

4. **`/bot-service/*` is gone from the master.** Safe for a split stack, since the bot talks to `bot-api:5000` (`BACKEND_API_URL=http://bot-api:5000/api` in `docker-compose.bot.yml`). But anything home-grown that called `/bot-service/*` on the master will break.

5. **`ADMIN_BACKEND_URL` is gone.** bot-api no longer proxies through the master at all — it provisions onto nodes directly via `LinkedPanel` → `POST /api/federation/provision` (`services/admin_proxy.py` was deleted outright). The variable was removed from `docker-compose.bot.yml` and `.env.example`; it had been a mandatory `:?required` that nothing ever read. There are now **zero** references to it in the repo.

6. **The legacy monolithic stacks are dead — statement of fact, not a task.** `docker-compose.yml`, `docker-compose.prod.yml` and `docker-compose.staging.yml` all still set `PANEL_ROLE=master` alongside a local `xray` container and point the bot at `backend:5000`, where `/bot-service/*` no longer exists. They were **already** broken before this phase: since Phase 3b the master gets a `RemoteXrayGateway` and registers none of `sync_traffic` / `check_limits` / `parse_logs`, so a local `xray` is neither driven nor polled. **The monolithic install path does not currently work.** The user has deliberately deferred `prod` / `staging` / `install_*` ("we'll do it from scratch") — do not try to repair these files as a side quest.

### Deploy note — one `backend` image becomes four (Phase 3d)

This wave retires the single `panel-backend` image in favour of one image per role. Read all four points before rolling it out.

1. **`BACKEND_IMAGE` is gone.** Every split stack now refuses to start until `.env` gains `MASTER_IMAGE` / `WORKER_IMAGE` / `SUB_IMAGE` / `BOT_API_IMAGE` — intentional fail-loud, same pattern as `BOT_EVENTS_REDIS_URI`. Each host only reads its own variable, but `.env` is usually shared across hosts, so set all four everywhere.

2. **`ghcr.io/ivantopgaming/panel-backend` is retired.** Nothing builds or pushes it any more; existing tags stay pullable but stop receiving updates.

3. **Four versions replace one.** `versions.json` no longer has `backend`; it has `master` / `worker` / `sub` / `bot_api` instead, and bumping one alone rebuilds and republishes only that image. The rebuild fan-out a deployer needs to reason about: a change in `panel-core` rebuilds all **four** backend images; a change in `panel-sub` rebuilds **three** (`sub`, `master`, `worker` — both `roles/master.py` and `roles/worker.py` register the `subscription` blueprint, since the master and each node serve subscription links themselves); a change in `panel-adminapi` rebuilds **two** (`master`, `worker`); a change confined to `panel-master`, `panel-worker` or `panel-botapi` rebuilds **one**. `backend/Dockerfile` builds `master`/`bot-api` off `PANEL_PACKAGE`; `backend/Dockerfile.worker` builds `worker` and alone carries the Xray binary and protobuf stubs; `backend/Dockerfile.sub` builds `sub` and alone carries the `@panel/sub-page` bundle — see Python dependencies & Docker images above for the measured sizes. One consequence of that third file: a change confined to `frontend/packages/sub-page/**` is a **backend** release — bump `sub`, not `frontend_admin`/`frontend_node`.

   **That scoping is exact, and `ui-core` falls outside it — a `ui-core` change is a three-image release.** sub-page reaches into `ui-core` for exactly one thing: `packages/sub-page/src/index.css:1` imports `../../ui-core/src/fonts.css`, which pulls in the 20 self-hosted `ui-core/src/fonts/*.woff2` files. `admin` and `node` reach that same file transitively, through `@ui/index.css` (`ui-core/src/index.css:1`). So a Roboto subset regeneration — or any other edit to `fonts.css` or the `.woff2` files — must bump `sub` **and** `frontend_admin` **and** `frontend_node`. Any *other* `ui-core` change (`components/`, `hooks/`, `lib/`, `stores/`, or `index.css` itself) rebuilds the two frontend images only, since sub-page imports none of it. There is no shared Tailwind theme to reason about in either case: `ui-core` carries no `tailwind.config.js` at all, each of the three packages holds its own copy of the palette, and only `admin`'s and `node`'s configs scan `../ui-core/src` for class names. Bumping `sub` alone for a `fonts.css` change leaves the admin and node SPAs serving the old bundle indefinitely, and System → About reports them current because their version keys never moved — the same permanently-green-row failure as point 4 of the Phase 3e note below. This phase is the worked example: it changed `fonts.css`, and its `versions.json` bumps `sub`, `frontend_admin` and `frontend_node` together for exactly this reason.

   **Phase 6 is a standing exception to the three-image fan-out above, and it is deliberate.** It changed `panel-sub` — the subscription page moved out of a Python f-string into `frontend/packages/sub-page` — and bumped **`sub` alone out of the four backend images** (`frontend_admin` and `frontend_node` moved too, for the `fonts.css` reason just above — that is the normal rule, not the exception). `master` and `worker` were left un-bumped on purpose: they register the `subscription` blueprint but bake no bundle, so rebuilding them would hand them the new code and turn their browser branch from a rendered page into a 503. They stay pinned to the older `panel-sub` until they either gain a bundle or stop registering the blueprint. **Do not "fix" this drift by bumping them at some later unrelated release** — that release is exactly when the trap fires. See Subscription links for the topology and for why `SUB_DOMAIN` is required.

4. **The schema-bump lockstep rule still applies.** Per-role versions do not change the existing requirement to deploy master and all linked panels in the same wave when `CURRENT_DB_VERSION` changes.

### Deploy note — one `frontend` image becomes two (Phase 3e)

This wave splits the single frontend image into an admin SPA and a node SPA. Read all five points before rolling it out.

1. **`FRONTEND_IMAGE` is gone.** Both split stacks now refuse to start until `.env` gains `FRONTEND_ADMIN_IMAGE` (`docker-compose.master.yml`) / `FRONTEND_NODE_IMAGE` (`docker-compose.node.yml`) — same fail-loud pattern as `MASTER_IMAGE`/`WORKER_IMAGE`/etc. Each host only reads its own variable, but `.env` is usually shared across hosts, so set both everywhere.

2. **`ghcr.io/ivantopgaming/panel-frontend` is retired.** Nothing builds or pushes it any more; existing tags stay pullable but stop receiving updates. `versions.json` no longer has `frontend`; it has `frontend_admin` / `frontend_node` instead, each bumped and rebuilt independently.

3. **Deploying the wrong image on a host is now loud, not silent.** Before the split, one image served both roles and the UI gated master-only pages/API calls at runtime off the injected `PANEL_ROLE` — a misconfigured role just hid or showed the wrong tabs. Now each image is built for one role (`vite.config.ts`'s `__EXPECTED_PANEL_ROLE__`) and `main.tsx` calls `assertPanelRole()` before rendering anything: if the role in the server-rewritten `<meta name="panel-role">` tag doesn't match what the bundle was built for, the whole page is replaced with a red error box naming both the expected and actual role, and no further JS runs. Concretely, this means `FRONTEND_ADMIN_IMAGE` pointed at a `worker` host (or vice versa) now fails visibly on first paint instead of quietly serving a UI that calls endpoints the running role doesn't register.

4. **A host still running the retired `panel-frontend` image will never be told about this migration by the panel itself.** `useVersionStatus.ts` reads `latest?.[__FRONTEND_VERSION_KEY__]`, and an old bundle was built with `__FRONTEND_VERSION_KEY__` baked to `'frontend'`. Once this merges, `main`'s `versions.json` no longer has a `frontend` key — only `frontend_admin` / `frontend_node` — so `check_latest_version`'s 6-hourly fetch caches a `latest` object with no `frontend` field, `latest?.frontend` is `undefined`, `isNewer(undefined, current)` short-circuits to `false`, and the frontend row on System → About never lights up again. It fails quiet, not loud: no crash, no `vundefined`, just a permanently-green row on the exact release that retired the image it's reporting on. There is no technical fix — keeping a frozen `frontend` key in `versions.json` would make the old bundle report "up to date", which is less honest than silence. **Operators must be told out of band:** repoint `.env` to `FRONTEND_ADMIN_IMAGE` / `FRONTEND_NODE_IMAGE` (point 1 above) and pull, on the strength of the release notes alone — no in-panel prompt will ever appear on a host still running the old image.

5. **Until Phase 6 the node SPA did not work behind Caddy at all.** Point 3's role check shipped as an inline `<script>window.__PANEL_ROLE__=…</script>` that `entrypoint.sh` injected into `index.html`, and the CSP the panel route sets is `script-src 'self'` — the browser refused to run it, so `window.__PANEL_ROLE__` was always `undefined`, every host resolved to `master`, and the node image's `assertPanelRole('worker')` painted its red error box on a correctly-configured node. It only ever worked when the SPA was reached without Caddy in front of it. The role now travels in a `<meta name="panel-role">` tag that `entrypoint.sh` rewrites with `sed` (see Secret path injection) — an attribute, not a script, so the CSP has no opinion on it. **`FRONTEND_NODE_IMAGE` must be re-pulled at `v2.4.2` or later for a node's UI to load**; nothing older will.

## Configuration

Copy `.env.example` to `.env`. Key variables:
- `PANEL_DOMAIN`, `PROXY_DOMAIN`, `PANEL_SECRET_PATH` — routing/TLS/decoy.
- `SUB_DOMAIN` *(required for the subscription page)* — dedicated subscription domain. When set, subscription links are served as `https://<SUB_DOMAIN>/api/sub/u/<token>` (clean, no secret path) and `build_aggregate_sub_url` prefers it. Must be in the cert's SAN and in the backend container's env. Empty → subscriptions fall back to `PANEL_DOMAIN` + secret path, which routes to the **master**, and the master bakes no page bundle: client apps still get their raw configs, but a browser opening that link gets **503**. Since Phase 6 that is the whole difference between a deployment where the subscription page exists and one where it does not — see Subscription links above.
- `SECRET_KEY`, `PANEL_ADMIN_USER`, `PANEL_ADMIN_PASSWORD`.
- `XRAY_CORE_REF` — Xray-core version to compile into the **worker** image (`backend/Dockerfile.worker`'s build-arg) — the only one of the four per-role backend images that carries the Xray runtime (build-time only).
- `RATELIMIT_STORAGE_URI` — Redis URI for rate limiting.
- `BOT_EVENTS_REDIS_URI` — event-bus Redis URI for the `bot:events` channel (`redis://` or `rediss://`). Defaults to `RATELIMIT_STORAGE_URI`. **Required on the master and on every node** — both stacks run their own local `redis` container for rate limiting and the sub-cache, so the default would publish into a Redis with no subscriber, and the event would be marked delivered and lost for good. Point it at the shared data-tier Redis (`docker-compose.postgres.yml`): `redis://node:<REDIS_NODE_PASSWORD>@<data-vm>:6379/0` on a node (publish-only credential), `redis://panel:<REDIS_PANEL_PASSWORD>@<data-vm>:6379/0` on the master. `sub` and `bot` have no local Redis and can rely on the default. That Redis ACLs two users: `node` (`-@all +publish +select &bot:events`) and `panel` (`~* &* +@all -@dangerous` — data + pubsub + scripting, but no `FLUSHALL`/`CONFIG`/`KEYS`/`SHUTDOWN`/`DEBUG`/`INFO`). The bus crosses hosts and carries the ACL password plus `telegram_id`/`email` in cleartext — run it over a private network between hosts or over `rediss://`.
- `BACKEND_LOG_LEVEL` *(default INFO)* — backend log verbosity. Every API request (`app.requests`), scheduler job run with duration (`app.jobs`), and federation HTTP call is logged at INFO/DEBUG; `DEBUG` additionally echoes every SQL statement (`sqlalchemy.engine` + per-statement timings in `app.sql`). Slow thresholds: `BACKEND_SLOW_SQL_MS` (default 200) and `BACKEND_SLOW_REQUEST_MS` (default 1000) promote slow statements/requests to WARNING. The backend container has json-file log rotation (50 MB × 5).
- `*_IMAGE` — per-service image pins (mirrors `versions.json`). The backend is now four images, each pinned by its own variable: `MASTER_IMAGE` (`docker-compose.master.yml`), `WORKER_IMAGE` (`docker-compose.node.yml`), `SUB_IMAGE` (`docker-compose.sub.yml`), `BOT_API_IMAGE` (`docker-compose.bot.yml`) — `BACKEND_IMAGE` no longer exists outside the frozen legacy monolithic compose files. The frontend is likewise two images: `FRONTEND_ADMIN_IMAGE` (`docker-compose.master.yml`) serves the admin SPA, `FRONTEND_NODE_IMAGE` (`docker-compose.node.yml`) serves the node SPA — `FRONTEND_IMAGE` no longer exists outside the frozen legacy monolithic compose files. See the deploy notes below.

Bot configuration is **not** in `.env`. It lives in `SystemSetting` rows managed via **Bot → Settings** in the panel UI: `bot_token`, `admin_telegram_ids`, `bot_service_token`, YooKassa `shop_id` / `secret_key`, `display_timezone`. The bot container only needs two env vars: `BACKEND_API_URL` and `BOT_SERVICE_TOKEN`. Changes take effect within ~60s without restarting the bot.

**Local vs. production validation:** When `PANEL_DOMAIN` is a local hostname (`localhost`, `*.local`, or an IP literal), the app relaxes requirements: weak `SECRET_KEY` is allowed, default `admin:admin` credentials are allowed, `memory://` rate limiting is allowed. For any real domain, all three are enforced on startup and the app refuses to start if they fail.
