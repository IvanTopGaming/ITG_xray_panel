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

The backend is split into five per-role images and `docker compose build backend` no longer
works — the shared `backend/Dockerfile` now requires a `PANEL_PACKAGE` build-arg with no default,
so a plain build fails with `PANEL_PACKAGE build-arg is required`. It builds `panel-master`,
`panel-bot-api` and `panel-cron`; `panel-worker` and `panel-sub` each have their own file. Build the role you
need directly with the same invocation CI uses:
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
| `backend` (`master`) | Admin API only (gunicorn + gevent, single worker) — runs the `panel-master` image; no local Xray, no billing surface, no scheduler, since wave 3b **no subscription surface**, and since wave 4c-1 **no backup surface** (`/api/backup` and `/api/restore` are node-only) |
| `backend` (`worker`/node) | Same Flask app plus the local Xray driver — runs the `panel-worker` image, the only one of the five per-role images carrying the Xray binary and the generated protobuf stubs. Serves no subscription route since wave 3b |
| `backend` (`sub`) | Subscription links only, and the **only** role that serves them — runs the `panel-sub` image, which also serves the React subscription page baked in at `/app/ui`. It is a **writer** of the shared Postgres: the device ledger (`user_device`) is written here on every config request |
| `backend` (`bot-api`) | `/bot-service/*` and the whole billing surface — runs the `panel-bot-api` image |
| `frontend` (`docker-compose.master.yml`) | The admin SPA (full UI, incl. Bot/Panels/Statistics) served by Nginx — runs the `panel-frontend-admin` image |
| `frontend` (`docker-compose.node.yml`) | The node SPA (Dashboard/Routing/System only, no page of its own) served by Nginx — runs the `panel-frontend-node` image |
| `caddy` | Reverse proxy — caddygen-built native JSON, SNI routing on `:443` (caddy-l4), `:80→:443` redirect, TLS from mounted certs, decoy masquerade |
| `redis` | Rate limiting + sub-cache + bot pubsub channel |
| `socket-proxy` | Restricts Docker socket access to specific API ops |
| `bot` | Telegram bot (Aiogram, asyncio) — runs on the bot host |
| `cron` | Background jobs (`docker-compose.cron.yml`) — runs the `panel-cron` image on its own host next to the data tier: polls every node, renews free tariffs, replays undelivered bot events, prunes old rows, checks for releases. Publishes no ports and registers no blueprint; it is also the **only** service that migrates the shared Postgres schema |

Three networks: `panel-net` (frontend/backend/caddy + xray + bot — the only one with internet egress) plus two `internal: true` segments: `redis-net` (backend ↔ redis ↔ bot) and `dockersock-net` (backend ↔ socket-proxy). The split (formerly a single `control-net`) keeps the Docker-socket proxy reachable only by `backend` and denies internet to both `socket-proxy` and `redis`. Key volumes: `shared_config:/etc/xray`, `xray_logs:/var/log/xray`, `./db_data:/app/db`, `./certs:/root/cert:ro`. Published ports on `caddy`: `80:80`, `443:443` (TCP only — there is no `443/udp` / HTTP-3).

In the split Postgres deployment there are **five** Flask app factories (`panel_core.roles.{master,worker,sub,botapi,cron}`). Which one runs is decided by the gunicorn command, not by `PANEL_ROLE`: the variable is a declared expectation that `bind_role()` compares against the factory that actually started, refusing to boot on a mismatch (it was worth having when the master image still shipped `panel-sub`, so that pointing its command at `roles.sub` would boot the wrong role under the right image name; wave 3b removed that dependency, and the check stays as cheap insurance against the same class of mistake). Left unset, `bind_role()` fills it in itself. The five: `master` (default — admin API, no local Xray, and **no billing surface**: it registers neither the `billing` nor the `bot_service` blueprint) runs against Postgres via `DATABASE_URL`; `worker` — called a **node** below — has its own Xray, but (per `docker-compose.node.yml` / `.env.node.example`) has no `DATABASE_URL` at all, so it runs against its own local SQLite (`./db_data`) as a cache/fallback rather than sharing the master's Postgres; `sub` serves subscription links and is the only role that does — it is also the only role that enforces the device limit, and therefore a **writer** of the shared Postgres, not a reader; `bot` (bot-api) serves `/bot-service/*` **and the whole billing surface** — `/api/billing/checkout`, the YooKassa webhook, and the three payment crons; `cron` runs every background job that used to sit on the master and **owns the shared Postgres schema** — it is the only role that migrates it. A node and a Panel Federation `LinkedPanel` (see Panel Federation below) are two views of the same thing, not separate systems: the node is the process role (`PANEL_ROLE=worker`), while `LinkedPanel` is the row the master's Postgres uses to address it (url + `federation_token`). The master routes provisioning to a node through exactly that federation path — `TariffItem.panel_id` → `LinkedPanel` → `FederationClient.provision()` → `POST /api/federation/provision` on the node (`services/panel_proxy.py`, `api/federation.py`) — which is also *why* a node can't resolve `lang`/`renewable` itself: it has no Postgres access to `TelegramUser`/`Tariff`, only its own local SQLite.

**Two Redis instances, split by who needs the data — not by who asked for it first.** `RATELIMIT_STORAGE_URI` names the box's own Redis; `SHARED_REDIS_URI` names the data tier. The rule is a single sentence: **anything more than one role has to see lives in the shared one.** That is the `bot:events` bus, the node snapshots (`panel:<id>:{snapshot,status,last_poll}`), the `panel:refresh` nudge — and, of the subscription cache, its *invalidation* only. What stays local is rate limiting plus each role's own cached subscription responses, which are genuinely per-role: a node builds that response from its own SQLite and sub builds it from Postgres, so the same key would hold two different answers.

`extensions.py` exposes the two clients separately — `get_redis()` (local) and `get_shared_redis()` (shared, plus `new_shared_redis_subscriber()` for a blocking pubsub connection) — and `tests/test_redis_split.py` holds the line both textually and behaviourally. **There is no fallback between the two variables any more.** The old `BOT_EVENTS_REDIS_URI` defaulted to `RATELIMIT_STORAGE_URI`, which on the master and on every node meant publishing into a Redis with no subscriber: `PUBLISH` still returns success, so `delivered_at` was stamped and the replay cron never retried — the event was lost silently and permanently. `SHARED_REDIS_URI` is now demanded via `:?` by master, node, sub, bot and cron alike, so an unset value fails the `up` instead.

That data-tier Redis ACLs two users: `node` (`-@all +publish +select &bot:events` — publish-only into one channel, plus `select` so a non-zero DB index in the URI still connects) and `panel` (everything except `@dangerous` — no `FLUSHALL`/`CONFIG`/`KEYS`/`SHUTDOWN`/`DEBUG`). One consequence of that deliberate narrowness: a node cannot invalidate the sub host's cached subscription, so its `sub_cache.invalidate_*` calls log one line and give up. Harmless — those entries expire within `SUB_CACHE_TTL_SECONDS` (60) — and preferable to widening the one credential that makes a node safe to place in an untrusted segment. See Bot event recovery buffer and Configuration below.

### Backend (`backend/`)

**Where the code actually lives.** The backend is a uv workspace (`backend/pyproject.toml` → `[tool.uv.workspace] members = ["packages/*"]`) with **eight** distributions under `packages/`, all of which install files into the *same* namespace package `panel_core` (each one's `[tool.hatch.build.targets.wheel] packages = ["src/panel_core"]`). **Imports do not depend on which distribution a module ships from** — `panel_core.api.billing` and `panel_core.api.inbound` are written identically no matter that they come from different wheels:

| Distribution | Ships | Deps |
|---|---|---|
| `panel-core` | the shared foundation — everything not listed in the other seven rows | flask (+sqlalchemy/migrate/cors/limiter/apscheduler), gunicorn, gevent, psycopg2-binary, psycogreen, redis, pyjwt, requests, pyyaml, cryptography |
| `panel-adminapi` | `api/{auth,inbound,outbound,routing,statistics,system,federation,backup}.py` | `panel-core` + **psutil** |
| `panel-worker` | `xray/{local,engine,grpc_client}.py`, `services/stats.py`, `roles/worker.py` | `panel-core`, `panel-adminapi`, `panel-sub` + **docker, filelock, grpcio, grpcio-tools, protobuf** |
| `panel-master` | `api/{bot_admin,panels}.py`, `roles/master.py` | `panel-core`, `panel-adminapi`, `panel-sub` |
| `panel-sub` | `api/subscription.py`, `roles/sub.py` | `panel-core`, `panel-links` |
| `panel-botapi` | `api/{billing,bot_service}.py`, `services/billing.py`, `jobs/payments.py`, `roles/botapi.py` | `panel-core`, `panel-links` + **`yookassa>=3.0,<4.0`** |
| `panel-cron` | `jobs/{billing,panels}.py`, `roles/cron.py` | `panel-core` |
| `panel-links` | `services/share_links.py` — one share link (`vless://`, `vmess://`, `trojan://`, `ss://`) per (inbound, client), plus the stream-settings extractors | `panel-core` |

**`yookassa` is a dependency of `panel-botapi` only** — it is not in `panel-core`'s dependency list, and `uv sync --package panel-core` does not install it. Importing `panel_core.roles.master` leaves `yookassa` out of `sys.modules`; only `panel_core.roles.botapi` pulls it in. Keep it that way: never import `yookassa` (or `panel_core.services.billing`) from a `panel-core` module.

**`panel-sub` used to be a dependency inversion for the master and worker — that is now history.** Through Phase 3c-3, `roles/master.py` and `roles/worker.py` still shipped from `panel-core` while registering the `subscription` blueprint, which ships from `panel-sub`; `panel-sub` declares `dependencies = ["panel-core"]`, so that reverse edge could not be declared without a workspace cycle, and `uv sync --package panel-core` alone could not build either role (`ImportError: cannot import name 'subscription' from 'panel_core.api' (unknown location)`). The `panel-master`/`panel-worker` cut resolved it: `roles/master.py` now ships from `panel-master` and `roles/worker.py` from `panel-worker`, and both declare `panel-sub` (and `panel-adminapi`) as ordinary dependencies. **`uv sync --package panel-core` now yields a buildable core on its own, and `ALLOWED_INVERSIONS` is empty** — the recorded exit criterion of the cut has been met. `dispatch.py` still ships from `panel-core` and still imports `roles/{sub,botapi,master,worker}` to dispatch to them; that edge lives in the separate, permanent `ROLE_DISPATCH_EXEMPTIONS` set (now four entries, one per role), not in `ALLOWED_INVERSIONS`. `panel-worker` and `panel-botapi` are the two distributions genuinely absent from the master's import graph.

**Import direction between distributions is guarded** (`tests/test_distribution_imports.py`). Because `panel_core` is one namespace package, an import statement says nothing about which wheel the target ships from — `from panel_core.services.billing import apply_payment` inside a `panel-core` module reads like a local import while actually inverting the dependency graph and pulling the `yookassa` SDK into every image. The guard resolves each `panel_core.*` import to its owning distribution and requires that owner to be inside the importer's **declared** dependency closure, read from the `pyproject.toml` files rather than hardcoded. The `yookassa` guard in `tests/test_workspace_layout.py` does **not** cover this: it matches literal `import yookassa` statements and never follows a `panel_core.*` edge. Two exemption sets exist, each with its own rationale in the file: the now-empty `ALLOWED_INVERSIONS` above, and `ROLE_DISPATCH_EXEMPTIONS` — `dispatch.py`'s `PANEL_ROLE` branches import `roles/{sub,botapi,master,worker}` *inside* `create_app()`, so each edge is only traversed on a host that installs that distribution by definition. That one is structural and permanent, and holds only while those imports stay function-level (separately asserted).

Every `app/…` path in the list below is shorthand for `backend/packages/<dist>/src/panel_core/…` — e.g. `app/models.py` is `packages/panel-core/src/panel_core/models.py`, imported as `panel_core.models`; `app/api/billing.py` is `packages/panel-botapi/src/panel_core/api/billing.py`, imported as `panel_core.api.billing`.

**`panel_core` is a namespace package (PEP 420).** Neither it nor its splittable subpackages (`api/`, `services/`, `jobs/`, `roles/`, `xray/`, `data/`) carries an `__init__.py`, which is what lets the eight distributions above ship into the same import root. This is no longer hypothetical: `panel_core.__path__` has **eight** contributions today, and every cut from the original three to today's eight changed **zero** call-site import statements outside the moved modules themselves. Consequences you must not undo (guarded by `tests/test_namespace_packages.py`, `tests/test_workspace_layout.py`, `tests/test_xray_facade.py`, `tests/test_bootstrap.py` — the workspace guard also fails on a module shipped by two distributions at once, and on a workspace member with Python code that no guard scans):
- **Importing `panel_core` runs no code.** What the deleted `__init__.py` files held now lives in explicit modules: `bootstrap.py` (`bootstrap_gevent()` — `gevent.monkey.patch_all()` + `patch_gevent_psycopg()`), `dispatch.py` (`create_app()`, the `PANEL_ROLE` → role-module dispatcher) and `xray/facade.py` (the gateway shims `has_local_xray`, `generate_config_file`, `restart_xray_container`, `stream_xray_logs`, `update_geo_db`, `_api_add_user_grpc`, `_api_remove_user_grpc`). Import them from those modules. Both of the old forms are **already broken today**, not merely fragile under a future split: `from panel_core.xray import generate_config_file` raises `ImportError: cannot import name 'generate_config_file' from 'panel_core.xray' (unknown location)` and `from panel_core import xray` + `xray.generate_config_file` raises `AttributeError`, because a namespace package owns no `__init__.py` and so re-exports nothing. The guard (`tests/test_xray_facade.py`) exists to stop either form being re-introduced — it is not a pre-emptive check against a split that has not happened yet. `xray/facade.py` ships from `panel-core` and dispatches to whichever gateway is bound at runtime; the local implementation it shims, `LocalXrayGateway`, lives in `xray/local.py` and ships from `panel-worker` — the only role with a local Xray to gate.
- **gevent patching is now every entry point's own job.** `run.py` (dev) calls `bootstrap_gevent()` on its first lines; `tests/conftest.py` calls it before importing anything else from `panel_core`. In containers nothing in Python does it — gunicorn's own worker does: `GeventWorker.init_process()` calls `gevent.monkey.patch_all()` before `base.Worker.init_process()` reaches `load_wsgi()`. That holds only while the gunicorn command keeps `-k gevent` and stays **without `--preload`** (with `--preload` the arbiter imports the app in the unpatched master process before forking). `tests/test_compose_gunicorn_gevent.py` guards both conditions across all eight gunicorn commands in `docker-compose*.yml`.
- **psycopg is patched on every *role* path** regardless: `build_base_app()` calls `patch_gevent_psycopg()` itself, so all four roles get the gevent wait callback even though `bootstrap_gevent()` was never called in-process (`tests/test_bootstrap.py` parametrises that over all four). The one exception is `sqlite_to_pg.py`, which builds no Flask app and calls neither `bootstrap_gevent()` nor `patch_gevent_psycopg()` — it reaches Postgres as plain blocking psycopg2. That is the right mode for a one-shot CLI migration (there is no gevent hub to block), but it *is* a behaviour change the namespace conversion made: the script used to inherit the patch from the deleted `panel_core/__init__.py`, and nothing replaced that side effect. Do not describe the patch as universal.
- **Package data is reached through `panel_core/resources.py`, never through `__file__`.** `resources.data_file(name)` / `read_data_text(name)` resolve via `importlib.resources.files("panel_core.data")`, which on 3.12 returns a `MultiplexedPath` that searches *every* distribution contributing to the namespace. The `__file__`-relative form is the same defect class as the `instance_path` one and fails the same way: `api/bot_admin.py` did `os.path.join(os.path.dirname(__file__), "..", "data")`, which under a two-distribution **editable** install (production's mode — `uv sync --frozen --no-dev`) resolves into the *api* distribution's tree, where `data/` does not exist. It failed silently — `GET /api/bot/texts/keys` returned HTTP 200 with `{"keys": []}` and the Bot → Texts tab went blank, no error, no log line. `db_migration.py`'s bot-texts seeder had the same shape (`__file__` + `"data"`). A non-editable wheel merges both trees into one `site-packages/panel_core/` and hides all of it, so this only ever breaks in production's install mode. `tests/test_resource_paths.py` rejects any `__file__`-derived path segment naming `..` or a namespace subpackage.
- **`root_path` and `instance_path` are passed to `Flask` explicitly** (`app_base.py`: `Flask("panel_core", root_path=PACKAGE_ROOT, instance_path=INSTANCE_PATH)`). Flask derives `root_path` from the package's `__file__` (a namespace package has none) and `instance_path` via `_find_package_path`, whose namespace branch does a bare `next()` over the search locations and raises `StopIteration` as soon as more than one location contributes — so leaving either to auto-discovery would break the moment the package is actually split. `INSTANCE_PATH` is `sys.prefix/var/panel_core-instance`: `sys.prefix` is unambiguous no matter how many distributions contribute, while any formula derived from the package location is not. Production installs `panel-core` **editable** (`uv sync --frozen --no-dev` in `backend/Dockerfile`), so this changed the value from `/app/packages/panel-core/src/instance` — harmless, because nothing reads `instance_path`. The only way to make it meaningful is a *relative* sqlite `DATABASE_URL` (`sqlite:///panel.db`), which Flask-SQLAlchemy resolves against `app.instance_path`. Nothing reaches that path today. Three of the eight compose files set `DATABASE_URL` at all — `docker-compose.{master,sub,bot}.yml`, each as a pass-through `${DATABASE_URL:?…}` that the compose file itself does not constrain, and `.env.{master,sub,bot}.example` fill all three with a `postgresql+psycopg2://…` URI. `docker-compose.node.yml` deliberately sets none (see the role paragraph above): the worker falls through `db_config.database_uri()` to `sqlite:///` + `app_base.db_path()`, which is **absolute** (`$CWD/db/panel.db`, mounted from `./db_data`) and therefore never consults `instance_path`. So a relative sqlite URI would have to be set by hand, against the only three roles whose compose requires the variable and expects Postgres — it is reachable, but nothing in the repo produces it.

- `app/app_base.py` + `app/dispatch.py` + `app/roles/{master,worker,sub,botapi}.py` — Flask app factories; register blueprints, extensions, ProxyFix, APScheduler jobs per role
- `app/models.py` — SQLAlchemy models (22 total). Core: `Admin`, `Inbound`, `Client`, `Outbound`, `RoutingProfile`, `Balancer`, `SystemSetting`, `TrafficSnapshot`, `DomainStat`, `LinkedPanel`, `FederationConfig`, `UserDevice` (the device ledger, keyed by `telegram_id` — see Device limit). Billing/bot: `Tariff`, `TariffItem`, `UserTariffAccess`, `Payment`, `BotText`, `BotEvent`, `TelegramUser`, `NotificationLog`, `NotificationClaim`, `ProvisionReceipt` (the node-side idempotency ledger — see Panel Federation). **FK enforcement is OFF** — `extensions.py` sets WAL/synchronous/busy_timeout/temp_store but **not** `PRAGMA foreign_keys=ON`, so FK constraints are advisory (deleting a parent leaves dangling child refs rather than cascading/erroring; e.g. `delete_tariff_permanent` can orphan `Client.tariff_id`). Exception: deleting a `LinkedPanel` (`delete_panel`) or an `Inbound` (`delete_inbound`, local + remote-via-`panel_id`) app-level cascades the matching `TariffItem` rows through `services/tariffs.purge_tariff_items`, which also disables any tariff left with zero items — so a removed panel/inbound can no longer orphan a `TariffItem` and 500 provisioning.
- `app/extensions.py` — Shared Flask extensions (db, migrate, APScheduler, Flask-Limiter, SQLite PRAGMAs)
- `app/utils.py` — JWT helpers + auth decorators: `token_required` (admin JWT only), `bot_service_token_required` (bot service token only), `federation_token_required` (validates federation token from linked panels), `admin_or_federation_token_required` (admin JWT **or** federation token — exactly two, since wave 4a). The latter two support the Panel Federation system. There is no dual admin/bot decorator any more: `admin_or_bot_token_required` and the bot-token branch inside `admin_or_federation_token_required` were both removed once the bot stopped calling the admin API — see Auth below.
- `app/api/`
  - `auth` — login / logout
  - `inbound`, `outbound`, `routing`, `panels`, `federation`, `subscription`, `statistics`, `system` — core panel
  - `backup` — `GET /api/backup` + `POST /api/restore`, **registered only by `roles/worker.py`**; both copy a SQLite file, which the master (Postgres) does not have. See Auth below
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
  - `device_tracking.py` — HWID-aware device limit enforcement, keyed by `telegram_id` (see Device limit)
  - `billing.py` — YooKassa SDK wrapper, `create_checkout`, `apply_payment` (atomic claim via `UPDATE … WHERE status='pending'` to prevent double-provision)
  - `provisioning.py` — single gateway for tariff grants: extends an existing `Client` for the same (telegram_id, inbound_tag) or creates one; resets counters; clears `traffic_*` `NotificationLog`; proxies to linked panels via `panel_proxy`
  - `bot_events.py` — `publish(event_type, telegram_id, payload)`: dual-write to `bot_event` table and Redis pubsub channel `bot:events`. Marks `delivered_at` on successful Redis publish.
  - `notifications.py` — `evaluate_expiry`/`evaluate_traffic` classify a client into a warning bucket (3d/1d/1h/expired; 80%/95%/exhausted); `emit_if_new` dedups via `NotificationLog` and publishes the bare fact only (no `lang`/`renewable` — a node can't resolve those) with `node` (the node's own `PANEL_DOMAIN`) and `inbound_tag` in the payload; `claim_notification` is the Postgres-backed atomic cross-node claim behind `NotificationClaim` (see Bot event recovery buffer)
  - `version_check.py` — `fetch_latest` (6h cron on the **cron service**): pulls the published `versions.json` from GitHub, keeps it in a process-local `_CACHE` **and** persists it into the `latest_versions` `SystemSetting`. The persistence is what makes the indicator work at all since wave 2: the process that fetches (cron) and the process that renders System → About (master) are different containers, so an in-memory-only cache would leave the row permanently green. `get_latest()` prefers the stored row and falls back to `_CACHE`, so a role with no schema or no app context degrades instead of raising
  - `bot_status.py` — small cache for the bot's reported version/health surfaced in the UI
- `app/jobs/`
  - `billing.py` — `auto_renew_free_users` (free-tier renewal, pause+notify on archive/disable) — ships from `panel-cron`
  - `payments.py` — `poll_pending_payments` (30s webhook fallback), `reconcile_refunds` (1h refund-webhook fallback → `billing.handle_refund` revokes access), `cleanup_old_payments` (24h, cancels stuck pending + publishes notification)
  - `notifications.py` — `cleanup_bot_events`, `replay_undelivered_bot_events` (also registered on the worker role, not just master). There is no `send_expiry_notifications`/`send_traffic_notifications` cron — expiry and traffic warnings are emitted inline from `stats.py`'s `check_limits_and_reset` and `sync_traffic_stats` via `services/notifications.emit_if_new`
  - `panels.py` — ships from `panel-cron`. `poll_linked_panels` (10s health poll of every node), `poll_panel_now(panel_id)` (the same poll for one node, out of band) and `run_refresh_listener(app)`, the greenlet subscribed to the `panel:refresh` channel that calls it

### Frontend (`frontend/packages/`)

`frontend` is an npm workspace (`frontend/package.json`: `workspaces: ["packages/*"]`) of four packages, each with its own `package.json`, `vite.config.ts`, `tsconfig.json`, `index.html`, `tailwind.config.js` and `postcss.config.js` — there is no root-level `index.html`/`vite.config.ts`/`tailwind.config.js`/`postcss.config.js` any more; only `entrypoint.sh` and `nginx.conf.template` stay shared at `frontend/`. Neither `@panel/admin`'s nor `@panel/node`'s nor `@panel/sub-page`'s `package.json` declares a dependency on `@panel/ui-core` — there is no workspace dependency edge at all, only an alias: each package's `tsconfig.json` and `vite.config.ts` map `@ui` → `../ui-core/src` and `@` → the app's own `src` (so a bare `@/pages/Panels` inside `admin` can never resolve inside `ui-core`, and vice versa). That alias is necessary but not sufficient — a relative specifier crosses the same boundary without ever touching it, which is why the import direction is also enforced by a dedicated guard (`backend/tests/test_frontend_import_direction.py`) rather than by the alias or the dependency graph.

- `packages/ui-core/src/` — everything shared by the three apps (55 files, every file under the directory regardless of extension — 33 `.ts`/`.tsx`, `index.css`, plus `fonts.css` and the 20 self-hosted Roboto/Roboto Mono `.woff2` files added in Phase 6 when the Google Fonts CDN link was dropped): `pages/` (`Dashboard`, `Routing`, `System`, `Login` — the four pages every role has), `components/inbound/` (`InboundForm`, `UserForm`), `components/ui/` (`Select`, `Modal`, `ConfirmationModal`, `Button`, `Input`, `Switch`, `TagInput`), `components/layout/` (`Layout`, `Sidebar`, `AnimatedBackground`), `components/DisplayConfigLoader.tsx`, `hooks/` (`useLinkedPanels`, `useVersionStatus`), `lib/` (`api.ts` — axios client with auth interceptor; `types.ts` — TS interfaces for every API entity; `protocols.ts` — protocol + stream-settings definitions; `panelRole.ts`/`assertPanelRole.ts` — role gating, see the deploy note below; `panelBase.ts`, `datetime.ts`, `devices.ts`, `routing-validation.ts`, `utils.ts`, `version.ts`), `stores/` (Zustand stores for auth + log state), `index.css`.
- `packages/admin/src/` — admin-only surface (19 files, same counting rule as ui-core above — this one happens to be all `.ts`/`.tsx`): `App.tsx`, `main.tsx` (the entry points), and the master-only pages/components `pages/Statistics.tsx`, `pages/Panels.tsx` (federation management), `pages/Bot.tsx` (billing UI) plus `components/bot/` (`TariffsTab`, `TariffDrawer`, `TariffsTable`, `TariffRowMenu`, `UsersTab`, `UserDrawer`, `GrantsTab`, `PaymentsTab`, `PaymentStatusBadge`, `TextsTab`, `SettingsTab`, `TrialCard`) and `lib/bot.ts`.
- `packages/node/src/` — **has no page of its own**: just `App.tsx`, `main.tsx`, `vite-env.d.ts` (3 files, same counting rule as the two bullets above). `App.tsx` wires up only the shared `Dashboard`/`Routing`/`System`/`Login` pages from `ui-core` (`Routing` further gated by `hasLocalXray`, which is always true for this image) — every route with its own page component lives in `ui-core` or `admin`, never in `node`. **Node-only surface therefore arrives as a gated tab inside a shared page, not as a route:** wave 4b's federation card is a `System` tab that ships from `ui-core` and renders only when `isWorker` — three gates in the bundle (the tab entry, its body, and `enabled: isWorker` on the `GET /api/federation/config` query, without which a master would 404 on every visit to System) plus a fourth in the backend, since `roles/master.py` registers no `federation` blueprint. The node's route count stays four. `backend/tests/test_federation_card_is_node_only.py` pins all of it.
- `packages/sub-page/src/` — the subscription page a user opens in a browser, and the only package that is **not** an admin surface (18 files, same counting rule as the bullets above): `App.tsx`, `main.tsx`, `vite-env.d.ts`, `components/` (`Header`, `Hero`, `Summary`, `QrPanel`, `AppButtons`, `Nodes`, `Footer`, `Loading`, `ErrorState`), `hooks/useSubInfo.ts`, `lib/` (`deeplinks.ts`, `format.ts`, `i18n.ts`, `types.ts`), `index.css`. It has no router, no axios client and no auth store — it reads one endpoint, `GET /api/sub/u/<token>/info`. Three things set it apart from the two admin apps: it ships **no** `assertPanelRole()` call and reads no `panel-role` meta tag (it is served by Flask out of `panel-sub`, not by Nginx, and there is no role to get wrong); it carries **its own `index.css`** rather than importing `ui-core`'s, because that one applies `overflow-hidden` to `body` for the fixed-chrome admin layout and would make a scrolling page unreadable on a phone; and it is built into the `panel-sub` backend image by `backend/Dockerfile.sub` rather than into an Nginx image of its own. It still looks like the same product, but only one of the two reasons is actual sharing: `ui-core/src/fonts.css` (self-hosted Roboto) is imported by all three packages and is sub-page's **only** edge into `ui-core`, while the Tailwind theme is a *duplicated copy* — `ui-core` has no `tailwind.config.js`, each package declares its own palette, and sub-page's config does not even scan `../ui-core/src`. That distinction decides the release fan-out — see point 3 of the Phase 3d deploy note.

Each of the two admin apps bakes its role at build time (`vite.config.ts`'s `define: { __EXPECTED_PANEL_ROLE__ }`) and asserts it at runtime against the `<meta name="panel-role">` tag that `entrypoint.sh` rewrites in `index.html` at container start (read by `lib/panelRole.ts`'s `readInjectedPanelRole()`). A meta tag, not an inline script: the reverse proxy's CSP sets `script-src 'self'`, which blocks inline `<script>` outright — see the deploy note below.

### Caddy (`caddy/`)
- `routes.yaml` — declarative per-SNI routes (the only hand-edited Caddy config). Fields: `match` (SNI host, `${ENV}` interpolated), `upstream` (`host:port`), `tls` (terminate vs raw passthrough), `only_paths` (path-prefix allowlist → 404, implies `tls`). A route whose `match` is empty after interpolation is **dropped** (so an empty `SUB_DOMAIN` drops the subscription route).
- `caddygen/` — small Go program that reads `routes.yaml` + env and emits Caddy's **native JSON** (entrypoint runs `caddygen → caddy validate → caddy run`). See "TLS, Caddy & certificates" below.

### Telegram Bot (`tg_bot/`)
- `main.py` — aiogram entry: bootstraps `runtime_config` → builds `Bot` → starts polling + bot-events consumer; on runtime change (token/proxy hot-swap) it stops polling, closes the old aiohttp session, builds a new `Bot`, and restarts polling **without** restarting the consumer (consumer holds a Bot-accessor closure, not a fixed ref)
- `runtime_config.py` — polls `GET /api/bot/runtime-config` every 60s; emits a change event when bot_token / telegram_proxy_url shift
- `backend_client.py` — thin async HTTP wrapper around `/bot-service/*` endpoints
- `bot_events_consumer.py` — subscribes to Redis `bot:events`, dispatches `payment_*` / `access_*` / `expiry_notification` / `traffic_notification` / `texts_changed` / `user_*` events
- `i18n.py` — `BotText` cache, `t(key, lang, **kwargs)` formatter (missing key → `⟨key⟩`, falling back to the other language first)
- `middleware.py` — `LangMiddleware`: per-user language lookup, cache, invalidation on `user_language_changed`
- `handlers/user.py`, `handlers/catalog.py` — message + callback handlers. There is **no `handlers/admin.py`**: wave 4a removed the bot's whole admin surface (backup, restore, restart, server listing). Fleet management lives in the master panel only
- `keyboards.py`, `states.py`, `utils.py` — UI builders, FSM states, helpers
- `config.py` — env validation: `BACKEND_API_URL`, `BOT_SERVICE_TOKEN`, `BOT_LOG_LEVEL`

The bot is **backend-client** (not standalone) — it has no local SQLite. All state (users, languages, notifications, payments) lives in the panel's `panel.db`. **One Telegram token may only long-poll once**, so run the `bot` service against a single master; never start a second poller with the same token (it would 409 the first).

**Every user-facing screen is built from one response — `GET /bot-service/users/<id>/state`.** That response carries, per client, `up`/`down`/`limit_bytes`/`expiry_time`/`enable`/`inbound_label` (and `panel_name` for a client on a node), plus a `links` array of ready share links, plus the account's `sub_url` and aggregate `expires_at_ms`. So "Statistics" needs no second call, "Keys" prints `record["links"]`, and the QR button encodes that key's own link (the subscription screen's QR encodes `sub_url`). Do **not** add a path from the bot to the admin API to fill any of this in: bot-api serves 15 routes and none of the admin ones, which is precisely how phase 3c-2 broke all three screens at once — `tg_bot/api_service.py` kept calling `/api/inbounds`, `/api/panels`, `/api/stats/system`, `/api/sub/<uuid>`, every one 404'd, and the bot rendered the 404s as "no keys" / "unavailable" / "No active key found". `tg_bot/tests/test_no_admin_surface.py` fails on any module that reaches for one of those paths.

**`links` is empty for a client with no `panel_id`, and that is correct.** bot-api can build a link only for a client it sees in a node snapshot, where the node's own hostname comes from `LinkedPanel.url`. For a local `Client` row in bot-api's own Postgres there is no right hostname to use — the bot host is not a node — so the screen says "no link" rather than handing out a confidently wrong address. In a split topology such rows do not exist anyway (`_require_local_xray` has blocked creating them since phase 3b).

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

### Device limit

**One ledger, keyed by the Telegram account, enforced on exactly one role.** `UserDevice` is unique on `(telegram_id, hwid)`; `services/device_tracking.user_device_gate(telegram_id, headers)` registers or refreshes a row on every config request and answers `limit` once the account is over `device_limit_per_user` (both that and `device_limit_enabled` are `SystemSetting` rows). The gate runs on the **sub** role, because since wave 3b that is the only role serving subscriptions — which also makes sub a **writer** of the shared Postgres, so a read-only credential there breaks the hot path rather than degrading it.

Two things about the grain are deliberate and easy to undo by accident:
- **Nothing joins through `Client`.** The predecessor counted `ClientDevice → Client` on `Client.telegram_id`, so the budget was whatever the serving role's database happened to hold. On a node that meant its own clients only — a user with keys on three nodes had three independent budgets — and on sub it would have meant *zero*, since no `Client` row for a node-issued client exists in Postgres at all (`Client` has no `panel_id` and the master mirrors none). Both failures were silent. The ledger therefore stores `telegram_id` and nothing else identifying.
- **A client with no `telegram_id` has no device tracking.** Per-client and per-inbound limits are no longer enforced anywhere: `Client.device_limit` / `Inbound.device_limit` still exist and the UI still edits them, but the only gate is the global one, and it needs a Telegram account to count against. Admin-created keys are outside it.

The admin surface reads the same ledger: `GET /users/<telegram_id>/devices` and `DELETE /users/<telegram_id>/devices/<id>` (admin JWT), and `device_count` per client in `GET /api/inbounds` is that account's count — the same number the gate sees and the same number the subscription page shows. Node snapshots carry no `device_count` any more; a node cannot know it, since the ledger lives in a Postgres it never reaches.

### Background scheduler jobs

| Job | Interval | What it does |
|---|---|---|
| `sync_traffic` | 10s | Per-user up/down from Xray gRPC; upserts `TrafficSnapshot` via raw SQL `ON CONFLICT DO UPDATE`; emits `traffic_notification` inline at 80%/95%/exhausted (dedup via `NotificationLog`) |
| `check_limits` | 60s | Removes expired/over-limit users; emits `expiry_notification` inline at 3d/1d/1h/expired (dedup via `NotificationLog`) |
| `parse_logs` | 15s | Tails Xray access logs into `DomainStat` (skips bare IPs) |
| `cleanup_stats` | 24h | Runs on **master and worker** roles; deletes `DomainStat` rows > 90d |
| `poll_linked_panels` | 10s | Runs on the **cron service only**. Pings each enabled `LinkedPanel`; fresh `snapshot`/`status`/`last_poll` go to the **shared** Redis every poll, the Postgres row is written **only on status/error change** (the panels API overlays the Redis values). A `panel:refresh` message polls one panel out of band, without waiting for the next tick |
| `auto_renew_free_users` | 15m | Runs on the **cron service only**. Re-provisions due `billing='free'` grants; pauses + emits `access_paused` on tariff archive/disable (does **not** force-disable clients — they lapse via their own `expiry_time`) |
| `poll_pending_payments` | 30s | Runs on the **`bot` (bot-api) role only** — not the master; webhook fallback, reconciles pending YooKassa payments older than 30s, younger than 24h |
| `reconcile_refunds` | 1h | Runs on the **`bot` role only**; refund-webhook fallback — re-checks the most recent succeeded payments (≤30d, capped 200) and revokes access on any YooKassa now reports refunded (via `billing.handle_refund`) |
| `cleanup_old_payments` | 24h | Runs on the **`bot` role only**; cancels `pending > 24h` (and publishes `payment_cancelled` so users find out); deletes terminal records `> 90d` |
| `replay_undelivered_bot_events` | 60s | Runs on the **cron service** (over Postgres) **and on every worker** (over that node's own SQLite, which nothing central can reach). Re-publishes any `bot_event` row with `delivered_at IS NULL` and `created_at < now - 30s` |
| `check_latest_version` | 6h | Runs on the **cron service only**; fetches the published `versions.json` from GitHub and persists it into the `latest_versions` `SystemSetting`, which is what lets the master render the "update available" indicator on System → About from another container |
| `cleanup_bot_events` | 24h | Runs on the **cron service** (over Postgres) **and on every worker** (over its own SQLite); prunes delivered `bot_event` rows > 7d, undelivered > 30d, and `NotificationClaim` + `ProvisionReceipt` rows > 90d. The receipt window has to outlive every retry that could reach the node — the longest is `cleanup_old_payments` re-applying a >24h payment — so 90 days is far more than needed, on purpose |

### Backend error handling pattern
All API handlers follow a two-catch pattern. `ValueError` is the type for user-facing validation errors — propagated as HTTP 400 with the message shown to the user. Bare `Exception` means an unexpected server fault and returns HTTP 500 with a generic message. Always raise `ValueError` (not `Exception`) for input validation failures so the error reaches the user.

### Auth
Four decorators in `app/utils.py`:
- `token_required` — admin JWT only. Used on all `bot_admin` endpoints, `GET /api/inbounds`, every one of the ten `panels.py` handlers (wave 4b added `POST /panels/<id>/relink`), and the node-only `POST /api/federation/link-token` + `GET /api/federation/config`.
- `bot_service_token_required` — fixed token from `SystemSetting('bot_service_token')`, compared in constant time. Used on all `bot_service.py` endpoints + `/billing/checkout`, **and on nothing else**.
- `federation_token_required` — validates the `federation_token` from a linked panel's `FederationConfig`. Used on federation endpoints that remote panels call.
- `admin_or_federation_token_required` — accepts admin JWT **or** federation token, and only those two. Sixteen handlers: twelve in `inbound.py` (user/inbound CRUD **and the `/users/bulk-*` + `/users/reset-traffic` batch endpoints**, so linked panels can proxy operations and the master can fan a batch out to children), `/api/restart` and `/api/stats/system` in `system.py`, and — since wave 4c-1 — `GET /api/backup` + `POST /api/restore` in the node-only `backup.py`.

All three decorators stamp `g.auth_via` (`"admin"` / `"federation"`) on the way through, which is how `backup.py` can log *which credential* took a node's database and not merely that someone did. A federated backup or restore leaves a WARNING on the node; the node's own admin leaves an INFO.

**`GET /api/backup` and `POST /api/restore` live in their own blueprint that only `roles/worker.py` registers, so the master answers 404 on both.** They used to sit in `system.py` under `token_required` — admin JWT only — while `panels.py` proxied to them with nothing but an `X-Federation-Token`, so backing a node up from the master answered 401 from the first release and no path to it existed. Both halves are gone: the routes take the federation token now, and they are node-only, because both copy a SQLite file and the master keeps its data in Postgres. There, `/api/backup` answered `404 "DB not found"` (which reads as *your database is gone*) and `/api/restore` tore down the live Postgres pool, wrote the upload where nothing reads it, restarted the worker and answered `{"status": "restored"}` — a disaster-recovery path confirming a recovery that never happened. A cheap `is_postgres()` refusal (409, naming `pg-backup`) also guards the handlers themselves, because `docker-compose.node.yml` merely omits `DATABASE_URL` rather than forbidding it. **The master's own database is backed up by the `pg-backup` container in `docker-compose.postgres.yml`, never through the panel** — System → Maintenance says so where the buttons used to be.

**The bot service token opens `/bot-service/*` and `/billing/checkout` and nothing else — that is a wave-4a change, and it is bigger than it reads.** Two separate paths used to accept it on the admin API. The explicit one was `admin_or_bot_token_required` on seven endpoints (`GET /api/inbounds` plus all six in `panels.py`). The quiet one was a third branch **inside** `admin_or_federation_token_required` — `if _check_bot_service_token(token)` — which put another 14 endpoints behind the same token: inbound and user CRUD, all six batch operations, `/api/restart` and `/api/stats/system`. So a leaked bot token could create and delete any user and any inbound, on the master and through it on any node by `panel_id`. Both are gone, along with `_check_bot_service_token` itself. The branch was unreachable in practice only because `tg_bot/api_service.py` was broken — restoring the bot by handing it admin endpoints, the obvious-looking fix, would have reopened it. `tests/test_bot_token_scope.py` builds the master role's app and asserts 401 on all 21, plus both positive paths.

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
   - `provisioning.apply_tariff_for_user(..., operation_id=f"pay:{payment.id}")` → extends or creates a `Client` per `TariffItem`
   - Sets `status='succeeded'`, publishes `payment_succeeded` to `bot:events` with the `expires_at_ms` **the nodes reported**, which is what the user is shown
   - On provisioning exception, releases claim back to `pending` so the poll cron retries

`poll_pending_payments` (30s) is the fallback when the webhook never arrived; it targets payments aged 30s–24h and runs the same `apply_payment`.

**That retry is why `operation_id` is `pay:<payment_id>` and not a fresh value per attempt.** A multi-node tariff whose second node is down raises after the first node has already been extended, and nothing rolls it back; the payment goes back to `pending` and the cron re-runs the *whole* grant every 30 seconds for up to 24 hours. Before this contract that was harmless — the node assigned an absolute date, so a repeat was idempotent by accident. Now the node adds, and the only thing standing between a stuck payment and a user with several years of access is that every retry carries the same key. Keep the key derived from the payment, never from the attempt.

### Provisioning (`services/provisioning.py`)

`apply_tariff_for_user(telegram_id, tariff, *, source, operation_id)` is the **single gateway** for every grant path (admin grant, trial, paid webhook, free auto-renew). `operation_id` is mandatory — it is the idempotency key that travels to every node (see Panel Federation for what it is per entry point and why). For each `TariffItem`:
- If `item.panel_id` is set → `proxy_provision` to that linked panel with **`period_ms`, never a computed expiry** — the node adds the period to whatever the user still had. This role cannot compute that date: node-issued clients have no `Client` row here, so any expiry it derives is wrong by exactly the remainder it cannot see. That was the bug (a 10-day remainder plus a 30-day purchase yielded 30 days, and the ten paid days vanished at checkout).
- Else if a `Client` already exists for the same (telegram_id, inbound_tag): extend it — bump `expiry_time`, reset `up/down/last_reset_time`, refresh `limit_bytes`, set `enable=True`, clear `traffic_*` `NotificationLog` rows (so the new cycle's warnings can fire).
- Otherwise create a new `Client` with a unique email (`tg<id>_<inbound_tag>` or `_<hex6>` on collision).

**`expiry_time == 0` means "never expires" and is preserved on both branches.** Buying a period on top of unlimited access refreshes the traffic limit and `enable` but leaves the expiry at `0`; adding a period would silently demote the user to a 30-day plan. `NULL` is *not* the same value — it means a damaged row (see the reply check in Panel Federation) and is counted from `now`, so a corrupted client does not become permanent.

**The returned `expires_at_ms` comes back from the nodes**, not from this role's own arithmetic: `apply_payment` puts it into the `payment_succeeded` event and the bot shows it to the user, so computing it locally would report 30 days while the node wrote 40. Several nodes yield several dates; `_collapse_expiries` picks one by the same rule `_collect_tariff_holders` already uses for backfill — **`0` absorbs everything, otherwise take the max** (a plain `max()` is wrong precisely because unlimited sorts below every date).

Every call also clears that user's `NotificationClaim` rows for the tariff (`clear_notification_claims`), so the next expiry/traffic cycle can warn again after a renewal instead of staying suppressed by a stale cross-node claim.

Single `_sync_after_provision` call after the loop: regenerates Xray config (or gRPC-patches for vless/vmess fast-path), restarts container if needed, and invalidates the Redis sub-cache. `backfill_tariff` idempotently ensures every active holder has a key on every tariff inbound (local + remote) without touching existing keys — and it is the one caller that legitimately sends `expiry_ms` rather than `period_ms`.

### Panel Federation

A master panel manages remote *linked panels*. `LinkedPanel` rows store URL + a `federation_token`; `FederationConfig` is a singleton on the child storing the master's credentials. The master proxies user/inbound CRUD to linked panels via `services/panel_proxy.py` (`FederationClient`). `TariffItem.panel_id` optionally routes a tariff item to a specific linked panel — provisioning then creates the user there instead of locally. `poll_linked_panels` (10s) health-polls each panel — from the **cron service**, which since wave 2 is the single writer of both `LinkedPanel.status` in Postgres and the `panel:<id>:*` keys in the shared Redis. The thirteen `proxy_*` operations no longer fetch a snapshot themselves; they publish the panel id on `panel:refresh` and return, and the cron service polls that panel out of band (`_nudge_panel_refresh`). **Never `DEL` the snapshot key instead:** for the sub host a missing key does not mean "stale", it means "this panel has no remote clients", so it skips the panel entirely and a user who has just paid opens the link to a subscription with no node servers in it. Subscription links (`api/subscription.py`) can merge entries from linked panels visible to the requesting client (Redis-cached). Inbound CRUD endpoints accept admin JWT **and** federation tokens (`admin_or_federation_token_required`) so children can proxy operations back through the master.

**Linking a node and revoking its token are the same endpoint, and the master never issues either.** `POST /api/federation/link-token` (admin JWT, node only) mints a fresh single-use link token **and revokes whatever access the panel currently grants** — it nulls `federation_token` and `linked_at` unconditionally and reports `revoked` in its reply. There is no separate rotation route and no 409: before wave 4b the endpoint refused once `federation_token` and `linked_at` were both set, which made a linked node's token unrevocable except by editing `federation_config` over SSH. The token handed to the admin is `base64url("<panel_url>|<raw_token>")`, where `panel_url` comes from the node's own `PANEL_DOMAIN` + `PANEL_SECRET_PATH` (`_build_panel_url`, falling back to `request.host`) — so a wrong `PANEL_DOMAIN` on a node sends the master to the wrong address, and the failure only surfaces as a handshake timeout. The node's System → Link card shows that URL, which is the only place an admin can catch it.

On the master, `POST /api/panels/<id>/relink` (admin JWT) decodes that token, handshakes, and writes the new `federation_token` **and** URL into the **existing** `LinkedPanel` row. **Never re-link by deleting and re-adding the panel:** `delete_panel` runs `purge_tariff_items(TariffItem.panel_id == …)`, which removes every `TariffItem` of that panel and disables any tariff left with none — revoking a credential would cost live users their tariff layout. `tests/test_federation_token_rotation.py` asserts the `TariffItem` count survives; note that asserting the row's `id` is unchanged does **not** catch delete-and-add, because SQLite reuses the rowid when the deleted row was the only one (the guard leans on `created_at` and the tariff count instead). `relink` writes no status — the cron service owns `LinkedPanel.status`; it publishes on `panel:refresh` and lets the poll do it.

Between the revoke and the re-link the node is unreachable to the master **entirely**: not polled, not provisioned, not managed, and `poll_linked_panels` marks it `offline` with the 401 as `last_error`. Provisioning in that window raises, so a payment stays `pending` and `poll_pending_payments` re-applies it after the re-link.

`POST /api/federation/handshake` carries `@limiter.limit("30 per minute")` — it is the only unauthenticated route the node serves, and `Limiter` has no `default_limits`. Flask-Limiter is constructed without `swallow_errors`, so this also makes handshake fail with 500 when the node's own Redis is unreachable; it is the one federation route that now depends on it.

**The provisioning contract carries two semantics, and which one you send decides who computes the expiry.** `POST /api/federation/provision` accepts `period_ms` **or** `expiry_ms` — exactly one; both or neither is a `ValueError` → 400. `period_ms` means *extend*: the node computes `max(now, client.expiry_time) + period_ms` itself, which is the only place the arithmetic can be correct, because an orchestrator's database holds no `Client` rows for node-issued clients (`Client` has no `panel_id` and the master does not mirror them). `expiry_ms` means *assign that exact date*, and exists for `backfill_tariff`, whose meaning is "give this user the same expiry he already has on his other nodes" — sending a period there would hand him `held_until + period` and drift one tariff's dates apart per node. Do not "simplify" the endpoint down to one field.

`period_ms` additionally **requires an `idempotency_key`**, and the rule generalises: *a key is required exactly where the operation is not idempotent on its own.* Assigning an absolute date is idempotent by construction; adding a period is not, and the retries are routine — `poll_pending_payments` re-runs a partially-failed multi-node grant every 30s, and the nodes that already succeeded are never rolled back (`provisioning.py`'s remote loop raises on the first failure). The node stores the key with its own reply in `provision_receipt` (unique on `(idempotency_key, inbound_tag)`) and replays that stored reply on a repeat, adding nothing — the reply matters because `apply_payment` puts its `expires_at_ms` into the `payment_succeeded` event the bot shows the user. There are two layers here: the fast path reads the receipt before mutating, and a concurrent request that slips past it fails the unique constraint, rolls back and returns the winner's result. Removing either one alone leaves the suite green, so do not delete the `IntegrityError` branch as dead code.

Callers pass a **natural key per entry point** (`operation_id` on `apply_tariff_for_user`, mandatory): `pay:<payment_id>`, `renew:<grant_id>:<due_ts>`, `trial:<tg>:<tariff_id>`, `grant:<uuid>` / `gift:<uuid>`. The first two are stable across exactly the automatic retries that exist; the last two have no automatic retry, so a repeat is an admin's intent rather than a fault.

**There is no contract version, deliberately.** `handshake` used to return a hardcoded `panel_version: 15` that nothing read; it is gone, and `tests/test_api_federation.py` now fails if it comes back. Compatibility is guaranteed by deploying the whole fleet in one wave — backwards compatibility holds only within minor releases, and major ones do not offer it. What replaced the version is a **reply check**: `proxy_provision` raises a `ValueError` naming the panel if the node answers without an `expires_at_ms`, which is exactly what a node left on an older release does when handed `period_ms`. That converts a silent success into a loud refusal, but it cannot prevent the damage — the stale node has already written `NULL` into that client's `expiry_time`, and one such row makes `check_limits_and_reset` raise `TypeError` on every 60s run, so **that node stops enforcing expiry and traffic limits for everybody**. Update nodes in the same wave; this is not a "when convenient" item.

**Destructive user ops read a LIVE snapshot.** `block_user` / `unblock_user` / `revoke_tariff_from_user` in `bot_admin.py` enumerate the user's remote clients via `_remote_clients_by_telegram_id_live()` (which calls `fetch_panel_snapshot_live` per enabled panel), **not** the cached `get_panel_snapshot` — a stale/missing cache must never let a remote disable silently no-op. Panels that can't be reached are surfaced in the response's `panel_failures` (not skipped). `revoke_tariff_from_user` matches remote clients by the tariff's `(panel_id, inbound_tag)` items **and** `tariff_id` (mirroring the local match by `tariff_id`), so two tariffs sharing a remote inbound don't cross-disable. The read-only users UI still uses the cached `_remote_clients_by_telegram_id()`.

### Bulk user operations (cross-panel)

The Dashboard selection toolbar drives a set of batch endpoints in `api/inbound.py`, all `@admin_or_federation_token_required`: `POST /users/bulk-delete`, `/users/bulk-enable`, `/users/bulk-adjust-days`, `/users/bulk-adjust-traffic`, `/users/reset-traffic`, `/users/bulk-set-flow`.

Each request carries `users: [{tag, email, panel_id?}]`. `_split_users_by_panel` splits the batch into a local group and per-panel remote groups. Remote groups are forwarded to the owning linked panel's **identical** endpoint via `panel_proxy` (with `panel_id` stripped, so the child runs them purely locally — no recursion). Proxying is **best-effort**: an offline/erroring child is collected into an `errors[]` field in the response instead of failing the whole batch, and counts (`deleted`/`updated`/`skipped`) are summed across local + remote. The single-user reset path also honours `?panel_id=` for child routing.

### VLESS flow ↔ transport compatibility

XTLS Vision (`xtls-rprx-vision`) is only valid on raw-TCP with TLS or REALITY — it is incompatible with xhttp/ws/grpc/httpupgrade/splithttp/kcp/quic and with `security: none`. `_stream_supports_vless_flow(stream)` in `api/inbound.py` encodes that rule (`network == "tcp" and security in {tls, reality}`). Two call sites keep `Client.flow` consistent:
- `bulk-set-flow` toggles flow `""` ↔ `xtls-rprx-vision` (whitelisted by `ALLOWED_VLESS_FLOWS`); enabling on an incompatible inbound is **skipped** (counted in `skipped`), disabling is always allowed.
- `update_inbound` clears now-invalid `flow` on every client of an inbound when its transport/protocol is switched to something flow can't carry (e.g. to xhttp), before the config is regenerated.

### Bot event recovery buffer

`services/bot_events.publish` writes a `BotEvent` row to SQLite *first*, then attempts `redis.publish('bot:events', …)`. On successful publish it sets `delivered_at = now`. The `replay_undelivered_bot_events` cron (60s, runs on the cron service over Postgres and on every worker over its own SQLite) re-publishes any row older than 30 seconds with `delivered_at IS NULL`. Caveat: Redis `PUBLISH` succeeding with `subscriber_count=0` (e.g. bot is down) still marks `delivered_at` because we don't check the return code — the recovery buffer protects against Redis outages but **not** consumer outages. This is intentional (a temporary bot stop is the supported way to suppress a wave of grant notifications during bulk operations).

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
`api/subscription.py` serves `GET /api/sub/<uuid_str>` — UUID-keyed, so renaming `Client.email` does NOT break a user's existing app config. The response can merge entries from linked panels visible to the user. Cached in Redis with a configurable TTL (`subscription_update_interval_hours` SystemSetting). `build_aggregate_sub_url(token)` builds the link the bot/dashboard show — `https://<SUB_DOMAIN>/api/sub/u/<token>` — and returns **`None`** when `SUB_DOMAIN` is empty; `build_client_sub_url(client_id)` does the same for the per-UUID form the Dashboard's copy/QR buttons hand out. There is **no `PANEL_DOMAIN` fallback** since phase 8 wave 3b: it produced a link to the master, which now serves no `/api/sub/*` at all. The variable must be present on the **backend** container of every role that builds a link — master, node and bot-api all do, none of them serves one.

**The per-UUID route builds a node client's config from the snapshot, not from the node.** A `Client` row for a client issued on a node exists only in that node's SQLite, so the route first looks locally and then falls through to `_remote_pair_for_uuid`, which finds the client in the cached `panel:<id>:snapshot` and builds all three formats (raw base64, Clash, sing-box) out of it. The predecessor — `_try_proxy_sub_to_child`, an HTTP `GET /api/sub/<uuid>` against the owning node with `timeout=8` — is gone: a dead node used to stall a live user's request for eight seconds. The accepted cost is that `subscription-userinfo` counters are now up to `SUB_CACHE_TTL_SECONDS`+poll-interval stale rather than live from the node's own database; against the `profile-update-interval` of 24h announced to the client that is noise. **A missing snapshot now means a 404 on that route** — nothing else can answer it.

That same `/api/sub/u/<token>` URL serves two audiences off one route: a client app's User-Agent gets the raw config, a browser gets the **React subscription page** — `frontend/packages/sub-page`, built by `backend/Dockerfile.sub` and baked into `panel-sub` at `/app/ui` (override with `SUB_PAGE_DIST`), with its assets under `/api/sub/u/assets/…`. The page is a static bundle and holds no data of its own; it fetches `GET /api/sub/u/<token>/info` for the JSON it renders (traffic used/limit, expiry, per-node entries, deep-links). A missing bundle 503s the page and its assets **without** touching config delivery — the sub role's critical function stays alive. The server-rendered HTML page this replaced is gone; there is no `<!doctype html>` left in the Python.

**Only the `sub` role serves subscriptions at all.** `roles/sub.py` registers the `subscription` blueprint and no other role does; `panel-sub` is also the one image that bakes the page bundle, so the role that serves the routes is exactly the role that can render them. Until wave 3b `roles/master.py` and `roles/worker.py` registered the same blueprint with no bundle behind it, which cost three things at once: an unauthenticated endpoint on an admin host, a **three-image** rebuild for every edit to the subscription code, and a browser branch that answered 503 where a page was expected while the client-app branch kept returning configs — a failure quiet enough to survive a release. All three are gone, and `panel-sub` is no longer a dependency of `panel-master` or `panel-worker`. **`SUB_DOMAIN` is therefore load-bearing:** with it empty no subscription link is produced anywhere, for a browser or for a client app. Baking the bundle into `master`/`worker` instead was considered and rejected: three images would carry a Node build stage to serve a page two of them have no business serving.

### Custom Select component
`frontend/packages/ui-core/src/components/ui/Select.tsx` renders a portal-based dropdown instead of a native `<select>`. It synthesizes a `React.ChangeEvent<HTMLSelectElement>` in its `onChange`. When used with react-hook-form, always spread `{...register('fieldName')}` so the `name` prop is passed — react-hook-form looks up the field by `event.target.name` and silently ignores the change if `name` is missing or empty.

### Default outbounds
On startup, `direct` (freedom) and `block` (blackhole) outbounds are auto-created if missing. These are always re-enabled if disabled — do not delete them.

### Database migrations

**Exactly one service migrates each database, and which one is decided by ownership.** The shared Postgres is migrated by the **cron service and nothing else** (`roles/cron.py` → `app_base.migrate_schema`); a node's own SQLite is migrated by that node (`roles/worker.py`), because nothing central can reach a file on a node's disk. The master, sub and bot-api migrate nothing. The master still seeds *defaults* into an existing schema (`bootstrap_defaults` — admin row, `bot_service_token`, the `direct`/`block` outbounds) but calls `_require_schema()` first and **refuses to start on a virgin database**, naming the deploy order in the error. So the order is now load-bearing and loud: **data tier → cron → master, sub, bot-api**. Before wave 2 the master created the schema, which made sub and bot-api answer 500 until it had booted once, with nothing anywhere saying why.

For local development on SQLite this means `uv run python run.py` on an empty database fails the same way — run `uv run python migrate_db.py` first, or bring up the cron role once.

`panel_core.db_migration` (standalone entrypoint: `backend/migrate_db.py`) is a custom migration system (not Flask-Migrate). Current schema version is **`25`**, tracked via `PRAGMA user_version`. The script is idempotent — runs on every backend startup, uses `CREATE TABLE IF NOT EXISTS` for new tables and `ALTER TABLE ADD COLUMN` (with `_add_column_if_missing` guard) for column additions. All `ALTER`s are SQLite metadata-only (O(1)), so migration time is independent of row count. When adding a new table: add a `_ensure_<name>_table` function, call it from `migrate_sqlite_db`, bump `CURRENT_DB_VERSION`. **Retired tables are listed once, in `RETIRED_TABLES`**, and dropped by both migration paths from that one list (`_drop_retired_tables`); it currently holds `node_traffic_snapshot` and `client_device`, both retired by wave 3b. `create_all()` never removes a table, so a retirement that is not in that tuple simply lingers forever on every live database.

**The Postgres side is a different mechanism with a different reach, and the difference bites on columns.** `migrate_postgres_db` (`pg_migrate.py`) is `db.create_all()` + dropping FK constraints + recording `schema_version` + seeding bot texts. Its only `ALTER` is `DROP CONSTRAINT`. So a **new table** arrives on both databases by itself (`create_all` on the cron service for Postgres, and on a node before `migrate_sqlite_db` for SQLite), while a **new column on an existing table** reaches Postgres only on a virgin database — on a live one nothing adds it and the first query through the model raises `UndefinedColumn`. This has never fired because no column has been added to an existing model since the Postgres path appeared, and twice now the shape of a change was chosen to keep it that way: the wave-3a idempotency key is a table rather than a column on `Client`, and wave 3b's device ledger is the new table `user_device` rather than a `telegram_id` column on `client_device` — which would additionally have needed `client_id` to lose its `NOT NULL` and a new unique constraint, neither of which either path can deliver. Before any change that genuinely needs a column, teach `migrate_postgres_db` to diff the models against `information_schema` — in its own change, not bundled into the wave that needs it. Note that a **table drop** is not in the same bind: wave 3b added an explicit `DROP TABLE` to both paths, which is why retiring one is cheap while altering one is not.

Bot texts have their own version: `CURRENT_BOT_TEXTS_VERSION = 18`. A bump triggers a one-shot **force-reseed** (only when `stored < CURRENT`): it DELETEs the `_REMOVED_BOT_TEXT_KEYS` tuple (purging orphan rows for keys dropped from the YAML) and then upserts every `(key, lang)` pair from `app/data/bot_texts_defaults.yaml` (~74 keys × RU/EN). The upsert **preserves admin-edited rows** — `bot_text.customized` (set to `1` whenever an admin saves a text via Bot → Texts) is honoured by `ON CONFLICT … DO UPDATE … WHERE customized = 0`, so a force-reseed refreshes only untouched defaults and never reverts customizations. On the v19 migration that added the column, rows whose stored text already diverged from the YAML default are back-filled `customized=1` to protect pre-existing edits. When you remove a key from the YAML, append it to `_REMOVED_BOT_TEXT_KEYS` (the purge ignores `customized`, since a removed key is dead regardless). **Nothing else ever deletes a bot text**, so a key dropped from the YAML without that entry survives on every live database as an orphan row the admin can still edit in Bot → Texts; `tests/test_bot_texts_defaults.py::test_every_key_dropped_from_the_yaml_is_listed_as_retired` diffs the YAML against `HEAD` and fails on one. It compares against `HEAD`, not against the last release, so it catches the omission in the wave that made it — not later.

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
2. Update the matching line in every `.env.<host>.example` that pins that image, so deployers pin the new tag (edit by hand to match the `versions.json` change; the examples use the `v`-prefixed tag, `versions.json` does not). Most images live in exactly one host file, but `CADDY_IMAGE` is pinned in all four and has four places to go stale in — `backend/tests/test_image_targets.py` checks every file that declares a pin.
3. Merge to `main`. The release workflow triggers only when `versions.json` changes on `main`.
4. CI diffs the new `versions.json` against the previous commit and builds/pushes **only the services whose version string changed**. If only `xray_core_ref` changed it's a no-op; bump `worker` too to force a rebuild — it's the only image the Xray core ref affects.
5. CI does **not** commit anything back to `main`. There is no auto-bump commit.

Force-pushing rewrites history — CI can't diff against the old SHA and falls back to `HEAD~1..HEAD`. Avoid force-pushing `main`; use feature branches.

### Panel Federation deploy ordering
When the schema bumps (any `CURRENT_DB_VERSION` change), **deploy master and all linked panels in the same wave**. A master on a newer schema may push user/tariff structures that an older linked panel can't parse. Back up first — the data tier with the `pg-backup` container, each node from its card on the master's Panels page (`GET /api/panels/<id>/backup`, wave 4c-1) — then `docker compose pull && up -d` everywhere. **There is no `GET /api/backup` on the master**; it answers 404 and, before wave 4c-1, answered `404 "DB not found"`, so an older runbook naming it produced nothing.

### Deploy note — the payment surface moved to bot-api (Phase 3c-2)

This wave moves the entire billing surface off the master. Read all six points before rolling it out.

1. **The YooKassa webhook is no longer served by the master.** `/api/billing/yookassa/webhook` now exists **only** on the bot host — `roles/master.py` registers neither the `billing` nor the `bot_service` blueprint; `roles/botapi.py` registers both. **Repoint your YooKassa merchant dashboard's webhook URL to `https://<BOT_DOMAIN>/api/billing/yookassa/webhook` before the rollout**, or confirmations stop landing on the master's now-dead endpoint.

2. **The webhook is reachable again (Phase 3c-2a closed the gap).** The bot host runs its own Caddy, same pattern as the sub host: `docker-compose.bot.yml`'s `caddy` service publishes `80:80`/`443:443` and requires `BOT_DOMAIN` (`:?BOT_DOMAIN is required` — **the bot stack refuses to come up without it, by design**); `caddy/routes.yaml` carries a `bot` SNI route matching `${BOT_DOMAIN}` → `backend:5000` (the `bot-api` container aliases itself as `backend` on `bot-net`), terminates TLS, and allowlists only `/api/billing/yookassa/webhook` via `only_paths`.

   **Narrowing the route is not enough — the *host* has to be narrow too, and that is a property of the compose file, not of `routes.yaml`.** caddygen drops an SNI route only when its `${VAR}` interpolates to the **empty string** (`caddy/caddygen/config.go`); it has no notion of a host role, so every domain variable the Caddy container can see turns another route on. `${PANEL_DOMAIN:-}` does **not** pass an empty string — compose's `:-` substitutes the value from `.env` whenever the variable is *present* and defaults only when it is *absent*, and `PANEL_DOMAIN` is mandatory on a bot host (bot-api needs it for `sub_links.build_aggregate_sub_url` and `federation._build_panel_url`). `env_file: - .env` re-injects them all regardless of the `environment:` block. So `docker-compose.bot.yml`'s `caddy` service carries **`BOT_DOMAIN` and nothing else, and no `env_file`** — the rendered container holds exactly one variable. Because SNI is client-chosen and the box serves its cert for whatever name is asked, a stray `PANEL_DOMAIN` there would make `https://<PANEL_DOMAIN>/<PANEL_SECRET_PATH>/api/…` aimed at the **bot** box's IP land on bot-api, reaching `/api/billing/checkout` and all of `/bot-service/*` — the exact surface `only_paths` exists to withhold (both are token-protected, so this is defence in depth, not an open door). Two guards hold the line, and both now cover **all four** hosts rather than the bot alone (see "Deploy note — every host serves only its own domains" below): `backend/tests/test_compose_host_ingress.py` (runs in CI) asserts each host's Caddy selects exactly its own routes, and `caddy/caddygen/compose_test.go` renders each compose file's Caddy environment through caddygen — against a deliberately fat `.env` holding every domain — and asserts the rendered layer4 routes and HTTP servers.

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

5. **`ADMIN_BACKEND_URL` is gone.** bot-api no longer proxies through the master at all — it provisions onto nodes directly via `LinkedPanel` → `POST /api/federation/provision` (`services/admin_proxy.py` was deleted outright). The variable was removed from `docker-compose.bot.yml` and from the then-shared `.env.example`; it had been a mandatory `:?required` that nothing ever read. There are now **zero** references to it in the repo.

6. **The legacy monolithic stacks are dead — statement of fact, not a task.** `docker-compose.yml`, `docker-compose.prod.yml` and `docker-compose.staging.yml` all still set `PANEL_ROLE=master` alongside a local `xray` container and point the bot at `backend:5000`, where `/bot-service/*` no longer exists. They were **already** broken before this phase: since Phase 3b the master gets a `RemoteXrayGateway` and registers none of `sync_traffic` / `check_limits` / `parse_logs`, so a local `xray` is neither driven nor polled. **The monolithic install path does not currently work.** The user has deliberately deferred `prod` / `staging` / `install_*` ("we'll do it from scratch") — do not try to repair these files as a side quest.

### Deploy note — one `backend` image becomes four (Phase 3d)

This wave retires the single `panel-backend` image in favour of one image per role. Read all four points before rolling it out.

1. **`BACKEND_IMAGE` is gone.** Every split stack now refuses to start until `.env` gains `MASTER_IMAGE` / `WORKER_IMAGE` / `SUB_IMAGE` / `BOT_API_IMAGE` — intentional fail-loud, same pattern as `BOT_EVENTS_REDIS_URI`. Each host only reads its own variable, but `.env` is usually shared across hosts, so set all four everywhere.

2. **`ghcr.io/ivantopgaming/panel-backend` is retired.** Nothing builds or pushes it any more; existing tags stay pullable but stop receiving updates.

3. **Four versions replace one.** `versions.json` no longer has `backend`; it has `master` / `worker` / `sub` / `bot_api` instead, and bumping one alone rebuilds and republishes only that image. The rebuild fan-out a deployer needs to reason about (wave 2 added `panel-cron` as a fifth): a change in `panel-core` rebuilds all **five** backend images; a change in `panel-sub` rebuilds **one** (`sub` alone) since wave 3b took the `subscription` blueprint off master and worker and dropped `panel-sub` from their declared dependencies — that fan-out reduction is one of the three things the wave was for; a change in `panel-links` rebuilds **two** (`sub`, `bot-api`); a change in `panel-adminapi` rebuilds **two** (`master`, `worker`); a change confined to `panel-master`, `panel-worker`, `panel-botapi` or `panel-cron` rebuilds **one**. `backend/Dockerfile` builds `master`/`bot-api`/`cron` off `PANEL_PACKAGE`; `backend/Dockerfile.worker` builds `worker` and alone carries the Xray binary and protobuf stubs; `backend/Dockerfile.sub` builds `sub` and alone carries the `@panel/sub-page` bundle — see Python dependencies & Docker images above for the measured sizes. One consequence of that third file: a change confined to `frontend/packages/sub-page/**` is a **backend** release — bump `sub`, not `frontend_admin`/`frontend_node`.

   **That scoping is exact, and `ui-core` falls outside it — a `ui-core` change is a three-image release.** sub-page reaches into `ui-core` for exactly one thing: `packages/sub-page/src/index.css:1` imports `../../ui-core/src/fonts.css`, which pulls in the 20 self-hosted `ui-core/src/fonts/*.woff2` files. `admin` and `node` reach that same file transitively, through `@ui/index.css` (`ui-core/src/index.css:1`). So a Roboto subset regeneration — or any other edit to `fonts.css` or the `.woff2` files — must bump `sub` **and** `frontend_admin` **and** `frontend_node`. Any *other* `ui-core` change (`components/`, `hooks/`, `lib/`, `stores/`, or `index.css` itself) rebuilds the two frontend images only, since sub-page imports none of it. There is no shared Tailwind theme to reason about in either case: `ui-core` carries no `tailwind.config.js` at all, each of the three packages holds its own copy of the palette, and only `admin`'s and `node`'s configs scan `../ui-core/src` for class names. Bumping `sub` alone for a `fonts.css` change leaves the admin and node SPAs serving the old bundle indefinitely, and System → About reports them current because their version keys never moved — the same permanently-green-row failure as point 4 of the Phase 3e note below. This phase is the worked example: it changed `fonts.css`, and its `versions.json` bumps `sub`, `frontend_admin` and `frontend_node` together for exactly this reason.

   **Phase 6 needed a standing exception to that fan-out; wave 3b removed the reason for it.** Phase 6 moved the subscription page out of a Python f-string into `frontend/packages/sub-page` and bumped `sub` alone, deliberately leaving `master` and `worker` pinned to the older `panel-sub`: they registered the `subscription` blueprint but baked no bundle, so rebuilding them would have turned their browser branch from a rendered page into a 503. Wave 3b took the blueprint off both roles and removed `panel-sub` from their dependencies, so there is nothing left to drift — the exception is closed, and a `panel-sub` change is a one-image release for the ordinary reason. See Subscription links for the topology and for why `SUB_DOMAIN` is now load-bearing.

4. **The schema-bump lockstep rule still applies.** Per-role versions do not change the existing requirement to deploy master and all linked panels in the same wave when `CURRENT_DB_VERSION` changes.

### Deploy note — one `frontend` image becomes two (Phase 3e)

This wave splits the single frontend image into an admin SPA and a node SPA. Read all five points before rolling it out.

1. **`FRONTEND_IMAGE` is gone.** Both split stacks now refuse to start until `.env` gains `FRONTEND_ADMIN_IMAGE` (`docker-compose.master.yml`) / `FRONTEND_NODE_IMAGE` (`docker-compose.node.yml`) — same fail-loud pattern as `MASTER_IMAGE`/`WORKER_IMAGE`/etc. Each host only reads its own variable, but `.env` is usually shared across hosts, so set both everywhere.

2. **`ghcr.io/ivantopgaming/panel-frontend` is retired.** Nothing builds or pushes it any more; existing tags stay pullable but stop receiving updates. `versions.json` no longer has `frontend`; it has `frontend_admin` / `frontend_node` instead, each bumped and rebuilt independently.

3. **Deploying the wrong image on a host is now loud, not silent.** Before the split, one image served both roles and the UI gated master-only pages/API calls at runtime off the injected `PANEL_ROLE` — a misconfigured role just hid or showed the wrong tabs. Now each image is built for one role (`vite.config.ts`'s `__EXPECTED_PANEL_ROLE__`) and `main.tsx` calls `assertPanelRole()` before rendering anything: if the role in the server-rewritten `<meta name="panel-role">` tag doesn't match what the bundle was built for, the whole page is replaced with a red error box naming both the expected and actual role, and no further JS runs. Concretely, this means `FRONTEND_ADMIN_IMAGE` pointed at a `worker` host (or vice versa) now fails visibly on first paint instead of quietly serving a UI that calls endpoints the running role doesn't register.

4. **A host still running the retired `panel-frontend` image will never be told about this migration by the panel itself.** `useVersionStatus.ts` reads `latest?.[__FRONTEND_VERSION_KEY__]`, and an old bundle was built with `__FRONTEND_VERSION_KEY__` baked to `'frontend'`. Once this merges, `main`'s `versions.json` no longer has a `frontend` key — only `frontend_admin` / `frontend_node` — so `check_latest_version`'s 6-hourly fetch caches a `latest` object with no `frontend` field, `latest?.frontend` is `undefined`, `isNewer(undefined, current)` short-circuits to `false`, and the frontend row on System → About never lights up again. It fails quiet, not loud: no crash, no `vundefined`, just a permanently-green row on the exact release that retired the image it's reporting on. There is no technical fix — keeping a frozen `frontend` key in `versions.json` would make the old bundle report "up to date", which is less honest than silence. **Operators must be told out of band:** repoint `.env` to `FRONTEND_ADMIN_IMAGE` / `FRONTEND_NODE_IMAGE` (point 1 above) and pull, on the strength of the release notes alone — no in-panel prompt will ever appear on a host still running the old image.

5. **Until Phase 6 the node SPA did not work behind Caddy at all.** Point 3's role check shipped as an inline `<script>window.__PANEL_ROLE__=…</script>` that `entrypoint.sh` injected into `index.html`, and the CSP the panel route sets is `script-src 'self'` — the browser refused to run it, so `window.__PANEL_ROLE__` was always `undefined`, every host resolved to `master`, and the node image's `assertPanelRole('worker')` painted its red error box on a correctly-configured node. It only ever worked when the SPA was reached without Caddy in front of it. The role now travels in a `<meta name="panel-role">` tag that `entrypoint.sh` rewrites with `sed` (see Secret path injection) — an attribute, not a script, so the CSP has no opinion on it. **`FRONTEND_NODE_IMAGE` must be re-pulled at `v2.4.2` or later for a node's UI to load**; nothing older will.

### Deploy note — every host serves only its own domains (Phase 8 wave 1)

This wave changes **no image**. It changes the compose files and replaces the single `.env.example` with one per host, so the whole cost is on the deployer. Read all six points before rolling it out.

1. **`.env.example` is gone; there are five files now.** `.env.master.example`, `.env.node.example`, `.env.sub.example`, `.env.bot.example`, `.env.data.example`. Copy the one matching the box, onto that box only. A shared `.env` was never correct: `RATELIMIT_STORAGE_URI` must be the box's *own* Redis on the master and on a node and the *data tier* on the sub and bot hosts, and the old file carried both values at once — one live, one commented out — leaving the contradiction for the deployer to resolve. The per-host files carry no commented alternatives, so a variable that does not belong on a host is simply absent. **The legacy monolithic `docker-compose.{yml,prod,staging}.yml` and `scripts/install_{dev,prod}.sh` still reference `.env.example` and now have no example file at all** — they were already non-functional (see point 6 of the 3c-2 note) and stay frozen pending the from-scratch installer.

2. **The Caddy container on master, node and sub no longer receives `env_file: - .env`.** That line was re-injecting every variable regardless of the `environment:` block, so on a shared `.env` each of the three lit SNI routes for *every* domain — pointed at its own backend. Concretely: the master answered `https://<SUB_DOMAIN>/api/sub/…` from its own `backend:5000`, and a node answered `https://<PANEL_DOMAIN>/<PANEL_SECRET_PATH>/api/…` from **that node's** admin API. Everything behind those routes stayed token-protected, so this was defence in depth that had been designed and did not work — but SNI is chosen by the client and each box serves its certificate for whatever name is asked, so the narrowing has to happen at the compose file, not in `routes.yaml`. Each Caddy now gets exactly what its own routes need: master `PANEL_DOMAIN` + `PANEL_SECRET_PATH`; node `PANEL_DOMAIN` + `PANEL_SECRET_PATH` + `PROXY_DOMAIN`; sub `SUB_DOMAIN` alone; bot `BOT_DOMAIN` alone (unchanged since 3c-2a). `PANEL_SECRET_PATH` is genuinely not needed on the sub host — only the `panel` route's `api_path` references it.

3. **`SUB_DOMAIN` is now required on the bot host, via `:?`.** It had been reaching bot-api through `env_file` alone while the `environment:` block listed `PANEL_DOMAIN` and `PANEL_SECRET_PATH` — that is, everything needed for the *wrong* answer and nothing for the right one. bot-api builds every subscription link the bot hands a user, in its own process, out of its own environment (`GET /bot-service/users/<id>/state` → `services/sub_links.build_aggregate_sub_url`); without `SUB_DOMAIN` it falls back to `PANEL_DOMAIN` + the secret path, which `caddy/routes.yaml` sends to the master, which bakes no page bundle. The failure is silent — a valid URL that 503s in a browser while client apps keep fetching configs — so it is now a start-up refusal instead. **Consequence to accept deliberately: a bot host in a deployment with no sub host will not start until `SUB_DOMAIN` is set.** That topology's subscription page was already dead; this makes it say so.

4. **`PROXY_DOMAIN` is gone from the master.** The master has had no `xray` service since phase 3b, so the decoy route pointed at a non-existent upstream — a request for the masquerade domain aimed at the master's IP was dropped rather than answered, which is the opposite of what a masquerade is for — and the `${PROXY_DOMAIN:?}` meant the master refused to start without a value for a route it should never have had. **Existing `.env` files keeping the variable are harmless; the master compose no longer names it.**

5. **The data tier's published ports default to `127.0.0.1`, not `0.0.0.0`.** `POSTGRES_BIND` / `REDIS_BIND` now fail closed when unset. Postgres was reasonably covered even exposed (`ssl=on`, `scram-sha-256`, `sslmode=verify-full` enforced on clients); the Redis was not and still is not — it runs with **no TLS at all**, so its ACL password and every `bot:events` payload (`telegram_id`, client e-mails) cross the wire in clear. **An existing `.env` that already sets `POSTGRES_BIND=0.0.0.0` keeps that behaviour** — compose defaults only apply when the variable is absent — so narrow it by hand to the data VM's private-network address as part of this rollout.

6. **Two guards were widened from one host to four**, plus one new one. `backend/tests/test_compose_host_ingress.py` (renamed from `test_compose_bot_ingress.py`) asserts every host's Caddy selects exactly its own routes and that none of them declares `env_file`; `caddy/caddygen/compose_test.go` renders each compose file's Caddy environment through caddygen against a deliberately fat `.env` and pins the resulting layer4 routes, HTTP servers and upstreams. `backend/tests/test_env_examples.py` is new and enforces both directions of the example files: every `${VAR:?…}` a compose file demands must be defined in that host's example, and no example may define a variable its own compose file never references — the second direction is what would have caught `SUB_DOMAIN` being load-bearing for bot-api while appearing nowhere in `docker-compose.bot.yml`.

### Deploy note — a sixth host, a renamed variable, and a master with no background work (Phase 8 wave 2)

This wave adds a machine, renames a mandatory variable on every existing one, and moves schema
ownership. All three break an existing deployment if skipped. Read all seven points before rolling
it out.

1. **There is a new host, and it is not optional.** `docker-compose.cron.yml` + `.env.cron.example`
   run the new `panel-cron` image (`CRON_IMAGE`, `versions.json` key `cron`). It publishes no ports,
   registers no blueprint and needs neither Caddy nor a certificate — all of its work is outbound. It
   carries the five jobs that left the master: `poll_linked_panels` (10s), `replay_undelivered_bot_events`
   (60s), `auto_renew_free_users` (15m), `cleanup_bot_events` (24h), `check_latest_version` (6h).
   **While it is down nothing polls the nodes**, so every `panel:<id>:snapshot` expires after 60s and
   the subscription shrinks to whatever the serving role holds locally; free tariffs stop renewing and
   undelivered bot events stop being replayed. That is the same exposure the master used to carry — it
   moved, it did not disappear.

2. **Put it next to the data tier, not on it.** The cron service is the first thing in this deployment
   that needs outbound HTTP to every node (for the poll, and for provisioning inside
   `auto_renew_free_users`). The data VM's whole value is that it has no outbound connections at all;
   co-locating them would spend that property to save a machine.

3. **`BOT_EVENTS_REDIS_URI` is now `SHARED_REDIS_URI`, with no fallback and one more host.** Rename it
   in `.env` on the master and every node, and **add** it to the sub and bot hosts, which previously
   relied on the default. All five stacks demand it via `:?` and will not start without it. The old
   name is not read anywhere any more — leaving it behind is a silent no-op, which is why the variable
   is mandatory rather than optional: a missing value must fail the `up`, not fall back.

4. **Only the cron service migrates the shared Postgres, so the master no longer creates the schema.**
   Deploy order becomes load-bearing: **data tier → cron → master, sub, bot-api**. On a virgin database
   the master now refuses to start and names that order in the error, instead of quietly creating the
   schema while sub and bot-api answered 500 until it had booted once. A node still migrates its own
   SQLite — nothing central can reach it. Nothing changes for an existing installation whose schema
   already exists.

5. **The master runs no scheduler at all now.** Its `-w 1` gunicorn limit exists only because
   APScheduler was pinned to a web worker; that reason is gone, but lifting the limit is deliberately
   left to a later wave rather than bundled here.

6. **`_get_panel_or_raise` no longer refuses on a stored `offline` status.** Provisioning now attempts
   the connection and fails on the actual result. This closes the case where a node marked offline just
   as the poller died stayed unprovisionable forever — a paid-for grant that could not be delivered.
   The cost is that provisioning to a genuinely dead node now waits for the timeout (up to 8s per item)
   rather than refusing instantly.

7. **A node's cache invalidation no longer reaches the sub host, on purpose.** Subscription-cache
   invalidation now also goes to the shared Redis so that an admin edit on the master or a purchase on
   bot-api clears the sub host's copy — the point of the change. A node cannot: its data-tier credential
   is publish-only by design. It logs one line and gives up, and the stale entry expires within
   `SUB_CACHE_TTL_SECONDS` (60). Widening the node ACL was rejected; that narrowness is what makes a node
   safe to place in an untrusted segment.

### Deploy note — the federation provisioning contract breaks, and there is no version to negotiate it (Phase 8 wave 3a)

This wave changes how a node is told to extend a subscription, bumps the schema, and deliberately
removes the only thing that looked like a compatibility check. **Master, bot-api, cron and every node
deploy in one wave.** Read all six points before rolling it out.

1. **A node left on an older image is not merely unsupported — it damages data on the first grant.**
   The orchestrator now sends `period_ms`; an old node looks for `expiry_ms`, does not find it, writes
   `NULL` into that client's `expiry_time` and answers HTTP 200. One such row makes that node's
   `check_limits_and_reset` raise `TypeError` on every 60-second run, so **the node stops enforcing
   expiry and traffic limits for every user it serves**, not just the damaged one. Repair is manual:
   set the row's `expiry_time` (0 for unlimited, or a real timestamp) once the node is updated.
   Provisioning itself recovers on its own — the payment stays `pending` and the poll cron re-applies it.

2. **What the panel does instead of a version check.** `proxy_provision` refuses a reply that carries no
   `expires_at_ms` and raises a `ValueError` naming the panel and telling the operator to update it. So the
   failure is loud and the payment is never marked succeeded — but the refusal lands *after* the stale node
   has written its `NULL`, because preventing that would require knowing the node's version before the call.
   `handshake`'s `panel_version` is **gone** (it was a hardcoded `15` that nothing read); compatibility is a
   property of deploying together, not of negotiation, and backwards compatibility is offered only within
   minor releases.

3. **`CURRENT_DB_VERSION` goes 23 → 24: a new `provision_receipt` table.** It is the node's idempotency
   ledger — the key of the operation plus the reply it produced. Nodes migrate their own SQLite on start;
   the shared Postgres gets the table from the cron service. Deploy order therefore still matters and is
   unchanged: **data tier → cron → master, sub, bot-api**, with nodes anywhere after the data tier.
   Nothing needs to be done by hand.

4. **Rolling back one host is not safe in either direction.** A new orchestrator against an old node is
   point 1. An old orchestrator against a new node sends `expiry_ms` with no key, which the new node
   still accepts (that semantics is kept for `backfill_tariff`) — so it works, but it silently restores
   the very defect this wave fixes: the purchased period replaces the remainder instead of extending it.
   If you must roll back, roll back the whole fleet.

5. **Users whose renewal landed during the broken era are not retroactively repaired.** This wave stops
   the remainder from being eaten; it does not refund the days already lost. If you want to compensate,
   do it with an admin gift after the rollout — and note that a gift now *adds* to what the user has,
   which is the point.

6. **Unlimited clients (`expiry_time = 0`) change behaviour, in their favour.** Buying a period on top of
   unlimited access used to demote it to a dated subscription; it now refreshes the traffic limit and
   leaves the access unlimited. If any of your tariffs relied on the old behaviour to put an expiry on a
   manually-created permanent client, it no longer will.

### Deploy note — subscriptions move to one role, `SUB_DOMAIN` loses its fallback, and the schema bumps (Phase 8 wave 3b)

This wave bumps `CURRENT_DB_VERSION` 24 → 25, takes the subscription surface off two roles out of
three, and deletes the fallback that let a subscription link exist without `SUB_DOMAIN`. **Master,
bot-api, cron, sub and every node deploy in one wave.** Read all eight points before rolling it out.

1. **An installation running on the `PANEL_DOMAIN` fallback loses subscriptions entirely, not just the
   page.** Before this wave an empty `SUB_DOMAIN` produced `https://<PANEL_DOMAIN>/<secret>/api/sub/u/<token>`,
   which routed to the master: a browser got 503 (no page bundle there) while client apps still fetched
   their configs, so the deployment looked half-working. The master now serves **no** `/api/sub/*` route
   at all, and `build_aggregate_sub_url` returns `None` rather than that URL. **Set `SUB_DOMAIN` on all
   four service hosts before rolling out** — `docker-compose.{master,node}.yml` now demand it via `:?`
   alongside sub and bot, so a host without it refuses to start instead of handing out dead links.

2. **What happens to a user holding an old link.** A link on `PANEL_DOMAIN` stops working the moment the
   master is updated: 404, for the app and for the browser alike. The app shows a failed update and keeps
   the last config it downloaded, so tunnels stay up until the key itself expires — the user is not cut
   off instantly, but they will never receive another update. There is no redirect and none is possible
   (the master cannot know the sub host's name from a request). **Users must be handed a fresh link.** The
   bot does that by itself: `sub_url` in `GET /bot-service/users/<id>/state` is rebuilt on every call, so
   "My subscription" shows the new URL as soon as bot-api restarts with `SUB_DOMAIN` set. Users who never
   open the bot need it sent to them. Links already on `SUB_DOMAIN` are unaffected — both forms keep working.

3. **The sub host's Postgres credential must be able to write.** `.env.sub.example` used to specify
   `panel_ro` and said so in a comment, because the device gate short-circuited on this role. It no longer
   does: every config request now registers or refreshes a `user_device` row. Give the sub host the same
   `panel` credential the master uses, or a role with `INSERT`/`UPDATE`/`DELETE` on `user_device`. A
   read-only user makes the gate raise on the hot path — configs stop being served, not merely counted.

4. **The device limit becomes global, and starts from zero.** It is now counted per Telegram account in
   the shared Postgres, not per client through the serving role's database. Two consequences: a user who
   had keys on three nodes had three independent budgets and now has one, so somebody at 2/2 on each of
   three nodes will suddenly be over a global limit of 2 and see `x-hwid-max-devices-reached` until they
   drop devices; and accumulated device rows are **not** migrated — `client_device` is dropped, the count
   restarts at zero, and devices re-register on their next config fetch. Raise `device_limit_per_user`
   before the rollout if the old per-node budget was the effective one.

5. **Per-client and per-inbound device limits stop being enforced.** `Client.device_limit` and
   `Inbound.device_limit` remain in the schema and in the forms, but nothing reads them for enforcement
   any more — the global setting is the only gate. Keys with no `telegram_id` (created by hand in the
   admin UI) therefore have no device tracking at all.

6. **Two tables are dropped: `client_device` and `node_traffic_snapshot`.** Both migration paths drop
   them from `RETIRED_TABLES`, so nothing is needed by hand. `node_traffic_snapshot` lost its last writer
   in wave 0 and never had a reader. If you want the device history, dump `client_device` before the
   rollout; it is not read by anything afterwards.

7. **A node stops seeding bot texts and stops minting a `bot_service_token`.** Both were dead on that
   role — the `bot_service` blueprint lives on bot-api — and the text seeding could publish
   `texts_changed` on the shared bus at node restart, making the bot re-read its texts against a version
   stored in that node's local table. Existing rows on a node are left alone; nothing reads them. The
   node keeps its own `Admin` row and its own Xray config generation, both of which belong there.

8. **The federation snapshot no longer carries `device_count`.** A node cannot know it any more. The
   master fills the number in from its own Postgres when it overlays a snapshot, so the Dashboard is
   unaffected — but a node left on an older image sends a field the master now ignores, and a master left
   on an older image shows `0` for every remote client. Same wave, same as everything else here.

### Deploy note — the bot loses its admin surface and the bot token stops opening the admin API (Phase 8 wave 4a)

This wave changes no schema and no federation contract, so it needs no fleet-wide lock-step. It does
remove capability an operator may be using today, and it narrows a credential. Read all five points.

1. **The bot's `/admin` menu is gone entirely.** Backup, restore, restart and the server list are no
   longer reachable from Telegram; `handlers/admin.py` and `api_service.py` are deleted. This removes
   nothing that worked: every one of those actions has been calling endpoints bot-api does not serve
   since phase 3c-2 and answering "0/N Online" with an empty server list. **Fleet management is the
   master panel's job from here on.** One gap worth knowing before you look for it: backing up a node
   *through* the master answers 401 and always has (the master sends only `X-Federation-Token` while
   the node's `/api/backup` takes an admin JWT), so until wave 4c-1 a node is backed up in its own
   panel with its own admin login. Wave 4c-1 closes that; see its deploy note below.

2. **The bot service token no longer opens any admin endpoint.** It reaches `/bot-service/*` and
   `/billing/checkout`, and nothing else. Twenty-one endpoints stop accepting it: `GET /api/inbounds`,
   the six `panels.py` handlers, inbound and user CRUD, the six batch operations, `/api/restart` and
   `/api/stats/system`. **Anything home-grown that authenticated to the panel with `BOT_SERVICE_TOKEN`
   breaks and must move to an admin JWT.** Nothing shipped in this repo did; the bot container is the
   only holder of that token. This is a real narrowing, not tidying: the token sits in `SystemSetting`
   in clear text, travels in every `pg_dump`, and reached user and inbound CRUD on the master and
   through it on every node.

3. **The three user screens work again, and they need the cron service up.** "Keys", "Statistics" and
   QR are rebuilt on `GET /bot-service/users/<id>/state`. For clients issued on nodes all three read
   the node snapshot, which only the cron service refreshes — **with the cron host down, snapshots
   expire after 60s and the bot shows an empty subscription**, exactly as the subscription page does.
   That is the wave-2 exposure, not a new one, but this is the first user-visible surface to depend on it.

4. **The QR flow changed shape.** There is no "choose a server" step: on a key's screen the QR is that
   key's own link, and the subscription screen gained a QR of the subscription URL. Four bot texts are
   deleted (`qr.select_title`, `qr.server_label`, `stats.key.per_server`, `stats.key.unavailable`) and one
   is added (`qr.no_link`); `CURRENT_BOT_TEXTS_VERSION` goes 17 → 18, which force-reseeds. Admin-edited
   texts are preserved as always (`customized = 1`), and the four deleted keys are purged regardless —
   if you had customised any of them, that wording is gone.

5. **Deploy `bot` and `bot-api` together.** They live in the same compose file, so this is automatic,
   but the direction matters if you stage it: a new bot against an old bot-api gets no `links` field and
   shows "no keys"; an old bot against a new bot-api is harmless. Also bump `sub`, `master`, `worker` and
   `cron` in the same rollout — a `panel-core` edit rebuilds all five backends, and `panel-links` (the new
   eighth distribution that carries share-link building) rebuilds `sub` alongside `bot-api`.

### Deploy note — the federation token becomes revocable, and a node finally has a linking screen (Phase 8 wave 4b)

This wave changes **no schema, no federation contract and no environment variable**, so it needs no
fleet-wide lock-step and no `.env` edit. It gives away one capability that did not exist and takes away
one that never worked. Read all six points.

1. **Nothing happens to an already-linked node on its own.** The revoke fires only when an admin presses
   the button on that node; until then `federation_config.federation_token` sits exactly where it was, and
   the master keeps polling and provisioning as before. Upgrading master, worker and both frontends changes
   no live state. This is the whole risk assessment — there is no migration and no start-up refusal.

2. **`POST /api/federation/link-token` on a node is now destructive, and the old 409 that made it safe is
   gone.** It used to answer `409 already linked` on a linked panel; it now revokes that panel's access and
   issues a fresh token in the same call. **Anything home-grown that called this endpoint to "check whether
   a token is pending" now disconnects the node from its master.** Nothing shipped in this repo did — no
   bundle called it at all before this wave (§50) — but a provisioning script might have. Use
   `GET /api/federation/config` for the read-only view; it is unchanged.

3. **The revoke → re-link window is a real outage for that node, by design.** Between pressing the button
   on the node and pasting the token into the master, the master cannot reach it at all: not polled (it goes
   `offline` with `HTTP 401` as `last_error`), not provisioned, not managed, no inbound or user CRUD. A
   purchase landing in that window raises, the payment stays `pending`, and `poll_pending_payments` (30s, on
   the bot host) re-applies it once the panel is re-linked — money is not lost, but the user waits. Keep the
   window short: issue the token and paste it in one sitting.

4. **Re-link, never delete-and-add.** The master's new `POST /api/panels/<id>/relink` (Panels → Relink)
   updates the existing row. Deleting the panel and adding it again looks equivalent and is not: `delete_panel`
   cascades `purge_tariff_items`, removing every `TariffItem` of that panel and **disabling any tariff left
   with no items** — live users lose the tariff. Relink also follows the node's address: the token carries the
   node's own `PANEL_DOMAIN`, so a node that moved to a new domain is repointed by the same action. That cuts
   both ways — pasting a token from a *different* node repoints this row, and its whole tariff layout, at that
   other box. The panel list shows the resulting URL; check it after relinking.

5. **A node whose own Redis is down can no longer be linked.** `POST /api/federation/handshake` gained a
   `30 per minute` limit, and Flask-Limiter is configured not to swallow storage errors, so an unreachable
   `RATELIMIT_STORAGE_URI` turns the handshake into an `HTTP 500` (verified by running it against a closed
   port). Handshake was previously the one route that touched nothing but its own database. The node's Redis
   is already mandatory and lives in the same compose stack, so this only bites a half-started node — but that
   is exactly the state you are in when first bringing a node up.

6. **A partial rollout fails loudly, in both directions.** A new master against an old node: pressing Relink
   sends a handshake the old node answers `401 no pending link token` (its admin cannot mint one — it still
   returns 409), and the master surfaces `Handshake failed: no pending link token`. A new node against an old
   master: the node revokes and issues fine, but the master has no Relink button, so the only paths are
   delete-and-add (point 4 — do not) or upgrading the master. Nothing is silently corrupted either way, and
   the node's stored token is untouched until a handshake succeeds.

   Bump `master`, `worker`, `frontend_admin` and `frontend_node` together: the `panel-adminapi` edit fans out
   to master and worker (the master ships that distribution even though it registers no `federation` blueprint),
   and the `ui-core` edit fans out to both frontend images. `sub`, `bot_api`, `cron`, `bot` and `caddy` are
   untouched.

### Deploy note — a node can finally be backed up from the master, and the master stops offering a backup it never had (Phase 8 wave 4c-1)

This wave changes **no schema, no federation contract and no environment variable**, so it needs no
fleet-wide lock-step and no `.env` edit. It restores a capability that never worked, removes two
buttons that lied, and — the part to read carefully — **widens what a leaked federation token can
do**. Read all six points.

1. **The federation token now reaches a node's entire database.** `GET /api/backup` and
   `POST /api/restore` accept it, so a holder can download the node's SQLite file whole and upload a
   replacement. The increment is smaller than it sounds and bigger than it looks. Smaller, because
   `/api/federation/snapshot` already handed the same token every inbound with every client, their
   UUIDs and their `telegram_id`s; what is new is the **rest** of the file — the node's own admin
   password hash, its routing profiles, its settings — and the ability to **overwrite** it. Bigger,
   because the token sits in the master's Postgres in clear text (it must, or the master could not
   present it), travels in every `pg_dump` of the data tier, and is not scoped per operation.

   **How to kill one (the wave-4b procedure, and the reason 4b shipped first):** on the node, System →
   Link → *Revoke access & issue token*; that nulls the current token on the spot and hands you a
   fresh single-use link token. Then on the master, Panels → the panel's card → *Relink*, and paste
   it. Never delete and re-add the panel instead: `delete_panel` cascades `purge_tariff_items`, which
   removes every `TariffItem` of that panel and disables any tariff left with none. Between the revoke
   and the relink the node is unreachable to the master entirely — polling, provisioning and CRUD all
   fail — so keep the window to one sitting; a purchase landing inside it stays `pending` and
   `poll_pending_payments` re-applies it afterwards.

2. **`/api/backup` and `/api/restore` no longer exist on the master, sub or bot-api — 404, not an
   error message.** They moved into a blueprint only `roles/worker.py` registers. Nothing is lost:
   on the master they never worked. `/api/backup` answered `404 "DB not found"` because it looked for
   a SQLite file a Postgres role does not have, and `/api/restore` accepted an upload, disposed of the
   live Postgres connection pool, wrote the file where nothing reads it, restarted the worker and
   answered `{"status": "restored"}`. **If any runbook of yours says "take a backup from the master
   panel before upgrading", it has been producing nothing this whole time** — see point 3.

3. **The master's own database is backed up by the `pg-backup` container in
   `docker-compose.postgres.yml`, on the data tier, and never through the panel.** System →
   Maintenance now says exactly that where the two buttons used to be. Nothing to configure: that
   container is already part of the data-tier stack.

4. **Backing a node up is a new button, not a restored one.** `Panels.tsx` never called
   `/panels/<id>/backup` or `/panels/<id>/restore` from any bundle — their only caller was the bot's
   admin menu, deleted in wave 4a. Each panel card now carries **Backup** (streams the node's file
   through the master straight into the admin's browser — it is never stored on the master) and
   **Restore** (file picker plus a confirmation that requires typing the panel's name, because
   pouring one node's database into another cannot be undone from the panel). What lands in the
   admin's Downloads folder is that node's keys and clients in the clear; treat it accordingly.
   `/panels/<id>/system-stats` and `/panels/<id>/restart` remain without any caller — their
   authorisation was always correct, there is simply no button.

5. **A node writes down who took its database.** Every federated `/api/backup` or `/api/restore`
   leaves a WARNING on the node naming the credential and the source address; the node's own admin
   leaves an INFO. No new table, no schema change — the container's existing json-file rotation
   (50 MB × 5) covers it.

6. **Deploy order does not matter, and a partial rollout degrades quietly rather than breaking.** An
   old master against a new node: the Backup button does not exist on the master, so nothing changes.
   A new master against an old node: pressing Backup answers 401, and the master now says so in words
   — *"Panel 'X' rejected this master's federation token. Issue a fresh link token on the node and
   relink the panel"* — which is misleading in this one case (the node is simply old), so update the
   node. Bump `master`, `worker`, `sub`, `bot_api`, `cron`, `frontend_admin` and `frontend_node`
   together: the `panel-core` edit (`utils.py`) fans out to all five backends, `panel-adminapi` to
   master and worker, `panel-master` to its own image, and the `ui-core` edit to both frontends.
   `bot` and `caddy` are untouched.

## Configuration

**There is no shared `.env.example`.** Each host copies its own: `.env.master.example`, `.env.node.example`, `.env.sub.example`, `.env.bot.example`, `.env.data.example` → `.env` on that box and nowhere else. One file could not be correct for every host even in principle — `RATELIMIT_STORAGE_URI` must point at the box's *own* Redis on the master and on a node and at the *data tier* on the sub and bot hosts, two mutually exclusive values of one variable, which the old single file carried at once (one live, one commented out) and expected the deployer to reconcile by hand. Each file now holds only what its host reads, with no commented alternatives. `backend/tests/test_env_examples.py` enforces both directions: every `${VAR:?…}` a compose file demands is defined in that host's example, and no example defines a variable its own compose file never references. Key variables:
- `PANEL_DOMAIN`, `PANEL_SECRET_PATH` — routing/TLS. `PANEL_DOMAIN` is **per-host by design**: on a node it must be *that node's* domain, because `services/notifications.py` also uses it as the node's identity in bot events.
- `PROXY_DOMAIN` — decoy SNI, raw-TCP passthrough to Xray (masquerade). **Node-only.** The master has had no `xray` service since phase 3b, so `docker-compose.master.yml` no longer names it at all.
- `SUB_DOMAIN` *(required — subscriptions do not work without it)* — the dedicated subscription domain, and since phase 8 wave 3b the **only** host any subscription link can name: `https://<SUB_DOMAIN>/api/sub/u/<token>` for a Telegram user, `https://<SUB_DOMAIN>/api/sub/<uuid>` for a single key. Must be in the cert's SAN and in the backend container's env. The old `PANEL_DOMAIN` + secret-path fallback is gone: it named the master, which no longer serves the route at all, so it turned an empty variable into a link that 404s in a browser while client apps kept working — quiet enough to ship. `build_aggregate_sub_url` / `build_client_sub_url` now return `None` instead. **All four service hosts demand it via `:?`, and only one of them serves it:** the sub host answers the routes; the master and each node read it purely to build the links their own Dashboard hands out (`api/inbound.py` → `sub_url` per client); bot-api reads it to build every link the bot sends a user (`GET /bot-service/users/<id>/state`). A host knowing its own domain is not enough — nothing asks the sub host what it is called.
- `SECRET_KEY`, `PANEL_ADMIN_USER`, `PANEL_ADMIN_PASSWORD`.
- `XRAY_CORE_REF` — Xray-core version to compile into the **worker** image (`backend/Dockerfile.worker`'s build-arg) — the only one of the five per-role backend images that carries the Xray runtime (build-time only).
- `RATELIMIT_STORAGE_URI` — **this box's own** Redis: rate limiting, plus this role's own subscription-response cache. On the master and on a node that is the stack's private `redis` container; on sub and bot-api, which run no Redis of their own, it points at the data tier. Read in exactly three places — the Flask-Limiter `storage_uri`, the start-up check in `app_base.py`, and `sub_cache` — and `tests/test_redis_split.py` fails on a fourth.
- `SHARED_REDIS_URI` — the **data-tier** Redis (`redis://` or `rediss://`), carrying the `bot:events` bus, the node snapshots and the `panel:refresh` nudge. **Required via `:?` on all five service hosts** — master, every node, sub, bot and cron. It replaced `BOT_EVENTS_REDIS_URI`, which defaulted to `RATELIMIT_STORAGE_URI`; that default is gone deliberately, see the two-Redis paragraph under Docker Services. Use `redis://node:<REDIS_NODE_PASSWORD>@<data-vm>:6379/0` on a node (publish-only credential) and `redis://panel:<REDIS_PANEL_PASSWORD>@<data-vm>:6379/0` everywhere else. The bus crosses hosts and carries the ACL password plus `telegram_id`/`email` in cleartext — run it over a private network between hosts or over `rediss://`.
- `BACKEND_LOG_LEVEL` *(default INFO)* — backend log verbosity. Every API request (`app.requests`), scheduler job run with duration (`app.jobs`), and federation HTTP call is logged at INFO/DEBUG; `DEBUG` additionally echoes every SQL statement (`sqlalchemy.engine` + per-statement timings in `app.sql`). Slow thresholds: `BACKEND_SLOW_SQL_MS` (default 200) and `BACKEND_SLOW_REQUEST_MS` (default 1000) promote slow statements/requests to WARNING. The backend container has json-file log rotation (50 MB × 5).
- `POSTGRES_BIND` / `REDIS_BIND` *(data tier)* — which host interface each port is published on. Both **default to `127.0.0.1`**, i.e. closed, so an unset value cannot publish the data tier to the internet; set them to the data VM's private-network address. Postgres is reasonably covered even when exposed (`ssl=on`, `scram-sha-256`, clients required to use `sslmode=verify-full`); **the Redis is not — it runs with no TLS at all**, so its ACL password and every `bot:events` payload (`telegram_id`, client e-mails) would cross the wire in clear.
- `*_IMAGE` — per-service image pins (mirrors `versions.json`). The backend is now five images, each pinned by its own variable: `MASTER_IMAGE` (`docker-compose.master.yml`), `WORKER_IMAGE` (`docker-compose.node.yml`), `SUB_IMAGE` (`docker-compose.sub.yml`), `BOT_API_IMAGE` (`docker-compose.bot.yml`), `CRON_IMAGE` (`docker-compose.cron.yml`) — `BACKEND_IMAGE` no longer exists outside the frozen legacy monolithic compose files. The frontend is likewise two images: `FRONTEND_ADMIN_IMAGE` (`docker-compose.master.yml`) serves the admin SPA, `FRONTEND_NODE_IMAGE` (`docker-compose.node.yml`) serves the node SPA — `FRONTEND_IMAGE` no longer exists outside the frozen legacy monolithic compose files. See the deploy notes below.

Bot configuration is **not** in `.env`. It lives in `SystemSetting` rows managed via **Bot → Settings** in the panel UI: `bot_token`, `admin_telegram_ids`, `bot_service_token`, YooKassa `shop_id` / `secret_key`, `display_timezone`. The bot container only needs two env vars: `BACKEND_API_URL` and `BOT_SERVICE_TOKEN`. Changes take effect within ~60s without restarting the bot.

**Local vs. production validation:** When `PANEL_DOMAIN` is a local hostname (`localhost`, `*.local`, or an IP literal), the app relaxes requirements: weak `SECRET_KEY` is allowed, default `admin:admin` credentials are allowed, `memory://` rate limiting is allowed. For any real domain, all three are enforced on startup and the app refuses to start if they fail.
