# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ITG Xray Panel is a full-stack VPN/proxy management panel for the [Xray-core](https://github.com/XTLS/Xray-core) proxy platform. It manages inbound/outbound proxy configurations, user accounts with traffic limits, routing rules, real-time traffic statistics, and a **YooKassa-backed billing system** with a fully customisable Telegram bot. A master panel can **federate** any number of remote panels.

**Stack:** Python 3.12 · Flask · gunicorn+gevent · SQLAlchemy · SQLite · Xray-core via gRPC · React + TypeScript + Vite · Aiogram 3 · Redis · Caddy (caddy-l4 SNI routing) · Docker Compose

## Commands

### Docker (primary workflow)
**There is no default `docker-compose.yml` any more, so every command needs `-f`.** The monolithic
`docker-compose.{yml,prod,staging}.yml` were deleted in wave 10 along with `scripts/install_{dev,prod}.sh`:
they set `PANEL_ROLE=master` beside a local `xray` container that no master has driven since phase 3b,
pointed the bot at `backend:5000` where `/bot-service/*` no longer exists, and downloaded a `.env.example`
that stopped existing in phase 8 wave 1. A bare `docker compose up` now fails with *no configuration file
provided* instead of bringing up a stack that cannot work. One file per role:

```bash
docker compose -f docker-compose.postgres.yml up -d   # data tier — Postgres + Redis + pg-backup
docker compose -f docker-compose.cron.yml     up -d   # cron — owns the shared schema, migrate first
docker compose -f docker-compose.master.yml   up -d   # master, sub, bot in any order after cron
docker compose -f docker-compose.sub.yml      up -d
docker compose -f docker-compose.bot.yml      up -d
docker compose -f docker-compose.node.yml     up -d   # node — any time after the data tier

# Rebuild and restart a single service after code changes:
docker compose -f docker-compose.bot.yml    build bot   && docker compose -f docker-compose.bot.yml    up -d bot
docker compose -f docker-compose.master.yml build caddy && docker compose -f docker-compose.master.yml up -d caddy
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

### Certificates
**Nothing to run: Caddy issues and renews them itself over ACME (wave 11).** There is no script, no
cron and no manual step on any of the four TLS-terminating hosts. `scripts/generate_certs.sh` and
`scripts/generate_local_cert.sh` were deleted in wave 10 and nothing replaced them — see **TLS, Caddy
& certificates** below for what the deployer must still get right (`:80` reachable, the domain
resolving to that box) and for the two optional variables, `ACME_EMAIL` and `ACME_CA`. The data
tier is the one exception and does not go through Caddy at all.
**There is no demo-data seeder any more.** `scripts/seed_demo.py` and `scripts/seed_bot_demo.py` were deleted in wave 10: both began with `from app import create_app`, and the package `app` stopped existing in phase 3c when the backend became the namespace package `panel_core` — so they had raised `ModuleNotFoundError` on their first import line for months while this file described them as a working tool. Writing a replacement is not a repair: after the split, demo data has to be seeded into **two** databases (the master's Postgres and a node's own SQLite), which is a different script.

## Architecture

### Docker Services
| Service | Role |
|---|---|
| `xray` | Xray-core proxy engine |
| `backend` (`master`) | Admin API only (gunicorn + gevent, single worker) — runs the `panel-master` image; no local Xray, no billing surface, no scheduler, since wave 3b **no subscription surface**, and since wave 4c-1 **no backup surface** (`/api/backup` and `/api/restore` are node-only). It registers the `statistics` blueprint but stores no traffic of its own: since wave 4d the five `/api/stats/*` handlers **refuse with 501 unless `?panel_id=` names a node**, and answer from that node over federation. Before 4d they answered from two permanently empty tables |
| `backend` (`worker`/node) | Same Flask app plus the local Xray driver — runs the `panel-worker` image, the only one of the five per-role images carrying the Xray binary and the generated protobuf stubs. Serves no subscription route since wave 3b |
| `backend` (`sub`) | Subscription links only, and the **only** role that serves them — runs the `panel-sub` image, which also serves the React subscription page baked in at `/app/ui`. It is a **writer** of the shared Postgres: the device ledger (`user_device`) is written here on every config request |
| `backend` (`bot-api`) | `/bot-service/*` and the whole billing surface — runs the `panel-bot-api` image |
| `frontend` (`docker-compose.master.yml`) | The admin SPA (full UI, incl. Bot/Panels/Statistics) served by Nginx — runs the `panel-frontend-admin` image |
| `frontend` (`docker-compose.node.yml`) | The node SPA (Dashboard/Routing/System only, no page of its own) served by Nginx — runs the `panel-frontend-node` image |
| `caddy` | Reverse proxy — caddygen-built native JSON, SNI routing on `:443` (caddy-l4), `:80→:443` redirect, TLS from mounted certs, decoy masquerade |
| `redis` | Rate limiting + sub-cache + bot pubsub channel |
| `socket-proxy` | Restricts Docker socket access to specific API ops |
| `bot` | Telegram bot (Aiogram, asyncio) — runs on the bot host |
| `cron` | Background jobs (`docker-compose.cron.yml`) — runs the `panel-cron` image on its own host next to the data tier: polls every node, resets granted traffic cycles, replays undelivered bot events, prunes old rows, checks for releases. Publishes no ports and registers no blueprint; it is also the **only** service that migrates the shared Postgres schema |

Three networks: `panel-net` (frontend/backend/caddy + xray + bot — the only one with internet egress) plus two `internal: true` segments: `redis-net` (backend ↔ redis ↔ bot) and `dockersock-net` (backend ↔ socket-proxy). The split (formerly a single `control-net`) keeps the Docker-socket proxy reachable only by `backend` and denies internet to both `socket-proxy` and `redis`. Key volumes: `shared_config:/etc/xray`, `xray_logs:/var/log/xray`, `./db_data:/app/db`, and `caddy_data:/data` — which since wave 11 holds the ACME account and every issued certificate, so deleting it costs a re-issue against Let's Encrypt's weekly limit. Published ports on `caddy`: `80:80`, `443:443` (TCP only — there is no `443/udp` / HTTP-3; `:80` is not merely a redirect any more, it is the only path the HTTP-01 challenge can take).

In the split Postgres deployment there are **five** Flask app factories (`panel_core.roles.{master,worker,sub,botapi,cron}`). Which one runs is decided by the gunicorn command, not by `PANEL_ROLE`: the variable is a declared expectation that `bind_role()` compares against the factory that actually started, refusing to boot on a mismatch (it was worth having when the master image still shipped `panel-sub`, so that pointing its command at `roles.sub` would boot the wrong role under the right image name; wave 3b removed that dependency, and the check stays as cheap insurance against the same class of mistake). Left unset, `bind_role()` fills it in itself. The five: `master` (default — admin API, no local Xray, and **no billing surface**: it registers neither the `billing` nor the `bot_service` blueprint) runs against Postgres via `DATABASE_URL`; `worker` — called a **node** below — has its own Xray, but (per `docker-compose.node.yml` / `.env.node.example`) has no `DATABASE_URL` at all, so it runs against its own local SQLite (`./db_data`) as a cache/fallback rather than sharing the master's Postgres; `sub` serves subscription links and is the only role that does — it is also the only role that enforces the device limit, and therefore a **writer** of the shared Postgres, not a reader; `bot` (bot-api) serves `/bot-service/*` **and the whole billing surface** — `/api/billing/checkout`, the YooKassa webhook, and the three payment crons; `cron` runs every background job that used to sit on the master and **owns the shared Postgres schema** — it is the only role that migrates it. A node and a Panel Federation `LinkedPanel` (see Panel Federation below) are two views of the same thing, not separate systems: the node is the process role (`PANEL_ROLE=worker`), while `LinkedPanel` is the row the master's Postgres uses to address it (url + `federation_token`). The master routes provisioning to a node through exactly that federation path — `TariffItem.panel_id` → `LinkedPanel` → `FederationClient.provision()` → `POST /api/federation/provision` on the node (`services/panel_proxy.py`, `api/federation.py`) — which is also *why* a node can't resolve `lang`/`renewable` itself: it has no Postgres access to `TelegramUser`/`Tariff`, only its own local SQLite.

**The shared Redis speaks TLS only, and a plain `redis://` across a network is refused at start-up (wave 8).** `docker-compose.postgres.yml` starts it with `--port 0 --tls-port 6379` off `./pg_certs/server.{crt,key}` — the same pair Postgres uses — so every client URI is `rediss://`. Before this the bus was authenticated (three ACL users, `default off`) and never encrypted, which is a weaker property than it sounds: one `panel:<id>:snapshot` carries each client's UUID, e-mail, telegram_id, traffic and expiry **and** the inbound's `realitySettings.privateKey`, so reading the wire yields both a usable `vless://` for somebody else's account and the node's server key. The deploy note had offered "a private network or `rediss://`" while the second did not exist in the image. `extensions.validate_shared_redis_uri` now refuses to boot on a cleartext URI whose host is not on this machine — a bare service name (`redis`) or loopback stays plain, because it never crosses a wire and that is how the master's own rate-limit store and an all-in-one deployment reach it. The cost is a real one and belongs with §10.6: **the data tier now needs a certificate for the hostname its clients use**, publicly-trusted for the zero-configuration path, or self-signed with the CA copied to every host.

**Two Redis instances, split by who needs the data — not by who asked for it first.** `RATELIMIT_STORAGE_URI` names the box's own Redis; `SHARED_REDIS_URI` names the data tier. The rule is a single sentence: **anything more than one role has to see lives in the shared one.** That is the `bot:events` bus, the node snapshots (`panel:<id>:{snapshot,status,last_poll}` plus the TTL-less `panel:<id>:{snapshot:last,last_poll:last}` — see Panel Federation), the `panel:refresh` nudge — and, of the subscription cache, its *invalidation* only. What stays local is rate limiting plus each role's own cached subscription responses, which are genuinely per-role: a node builds that response from its own SQLite and sub builds it from Postgres, so the same key would hold two different answers.

**Neither instance being reachable is allowed to refuse a request any more (wave 5d).** `build_base_app` sets `RATELIMIT_IN_MEMORY_FALLBACK_ENABLED`, so an unreachable storage moves the counters into the process — it does **not** switch the limits off. That distinction is the whole point and is guarded: `swallow_errors` would have removed the same 500 by removing `10 per minute` from the admin login and `30 per minute` from a node's handshake as well. Because every gunicorn command runs `-w 1`, one process is one host, so a degraded counter covers exactly the population the Redis-backed one did. flask-limiter logs `Rate limit storage unreachable - falling back to in-memory storage` once and `Rate limit storage recovered` when it returns, on its own. Before this, `RATELIMIT_STORAGE_URI` pointing at another machine — which it does on sub and bot-api, neither of which runs a Redis — meant a dead data tier answered **500 on every subscription request**, while everything else on that path (`sub_cache`) had been failing open all along.

`RATELIMIT_STORAGE_URI` is read at **app-build time**, not at import: `extensions.py` constructs `Limiter(key_func=…)` with no `storage_uri` and `app_base` puts `local_redis_uri()` into `app.config`. The reverse — which is what shipped until wave 5d — makes `app.config["RATELIMIT_STORAGE_URI"]` dead configuration, because flask-limiter resolves `self._storage_uri or storage_uri_from_config` and the constructor value is never empty. Nothing broke in production (a container's env is set before the process starts), but the storage could not be re-pointed in-process, so no test could exercise a dead one.

`extensions.py` exposes the two clients separately — `get_redis()` (local) and `get_shared_redis()` (shared, plus `new_shared_redis_subscriber()` for a blocking pubsub connection) — and `tests/test_redis_split.py` holds the line both textually and behaviourally. **There is no fallback between the two variables any more.** The old `BOT_EVENTS_REDIS_URI` defaulted to `RATELIMIT_STORAGE_URI`, which on the master and on every node meant publishing into a Redis with no subscriber: `PUBLISH` still returns success, so `delivered_at` was stamped and the replay cron never retried — the event was lost silently and permanently. `SHARED_REDIS_URI` is now demanded via `:?` by master, node, sub, bot and cron alike, so an unset value fails the `up` instead.

That data-tier Redis ACLs **three** users: `node` (`-@all +publish +select &bot:events` — publish-only into one channel, plus `select` so a non-zero DB index in the URI still connects), `bot` (`-@all +subscribe +psubscribe +unsubscribe +ping +select &bot:events`, wave 7 — the Telegram poller is the one process here that feeds untrusted internet input to something other than a web framework, and it needs to listen to one channel and nothing else; it used to hold `panel`, which reads every node snapshot and so every user's UUID, telegram_id, e-mail, traffic and expiry) and `panel` (everything except `@dangerous` — no `FLUSHALL`/`CONFIG`/`KEYS`/`SHUTDOWN`/`DEBUG`). bot-api keeps `panel`: it publishes and reads snapshots. One consequence of that deliberate narrowness: a node cannot invalidate the sub host's cached subscription, so its `sub_cache.invalidate_*` calls log one line and give up. Harmless — those entries expire within `SUB_CACHE_TTL_SECONDS` (60) — and preferable to widening the one credential that makes a node safe to place in an untrusted segment. See Bot event recovery buffer and Configuration below.

### Backend (`backend/`)

**Where the code actually lives.** The backend is a uv workspace (`backend/pyproject.toml` → `[tool.uv.workspace] members = ["packages/*"]`) with **eight** distributions under `packages/`, all of which install files into the *same* namespace package `panel_core` (each one's `[tool.hatch.build.targets.wheel] packages = ["src/panel_core"]`). **Imports do not depend on which distribution a module ships from** — `panel_core.api.billing` and `panel_core.api.inbound` are written identically no matter that they come from different wheels:

| Distribution | Ships | Deps |
|---|---|---|
| `panel-core` | the shared foundation — everything not listed in the other seven rows | flask (+sqlalchemy/migrate/cors/limiter/apscheduler), gunicorn, gevent, psycopg2-binary, psycogreen, redis, pyjwt, requests, pyyaml, cryptography |
| `panel-adminapi` | `api/{auth,inbound,outbound,routing,statistics,system}.py` | `panel-core` + **psutil** |
| `panel-worker` | `xray/{local,engine,grpc_client}.py`, `services/stats.py`, `roles/worker.py`, and — since wave 7 — the node-only `api/{federation,backup}.py` | `panel-core`, `panel-adminapi`, `panel-sub` + **docker, filelock, grpcio, grpcio-tools, protobuf** |
| `panel-master` | `api/{bot_admin,panels}.py`, `roles/master.py` | `panel-core`, `panel-adminapi`, `panel-sub` |
| `panel-sub` | `api/subscription.py`, `roles/sub.py` | `panel-core`, `panel-links` |
| `panel-botapi` | `api/{billing,bot_service}.py`, `services/{billing,tariff_delivery}.py`, `jobs/payments.py`, `roles/botapi.py` | `panel-core`, `panel-links` + **`yookassa>=3.0,<4.0`** |
| `panel-cron` | `jobs/{billing,panels}.py`, `roles/cron.py` | `panel-core` |
| `panel-links` | `services/share_links.py` — one share link (`vless://`, `vmess://`, `trojan://`, `ss://`) per (inbound, client), plus the stream-settings extractors | `panel-core` |

**`yookassa` is a dependency of `panel-botapi` only** — it is not in `panel-core`'s dependency list, and `uv sync --package panel-core` does not install it. Importing `panel_core.roles.master` leaves `yookassa` out of `sys.modules`; only `panel_core.roles.botapi` pulls it in. Keep it that way: never import `yookassa` (or `panel_core.services.billing`) from a `panel-core` module.

**`panel-sub` used to be a dependency inversion for the master and worker — that is now history.** Through Phase 3c-3, `roles/master.py` and `roles/worker.py` still shipped from `panel-core` while registering the `subscription` blueprint, which ships from `panel-sub`; `panel-sub` declares `dependencies = ["panel-core"]`, so that reverse edge could not be declared without a workspace cycle, and `uv sync --package panel-core` alone could not build either role (`ImportError: cannot import name 'subscription' from 'panel_core.api' (unknown location)`). The `panel-master`/`panel-worker` cut resolved it: `roles/master.py` now ships from `panel-master` and `roles/worker.py` from `panel-worker`, and both declare `panel-sub` (and `panel-adminapi`) as ordinary dependencies. **`uv sync --package panel-core` now yields a buildable core on its own, and `ALLOWED_INVERSIONS` is empty** — the recorded exit criterion of the cut has been met. `dispatch.py` still ships from `panel-core` and still imports `roles/{sub,botapi,master,worker}` to dispatch to them; that edge lives in the separate, permanent `ROLE_DISPATCH_EXEMPTIONS` set (now four entries, one per role), not in `ALLOWED_INVERSIONS`. `panel-worker` and `panel-botapi` are the two distributions genuinely absent from the master's import graph.

**Import direction between distributions is guarded** (`tests/test_distribution_imports.py`). Because `panel_core` is one namespace package, an import statement says nothing about which wheel the target ships from — `from panel_core.services.billing import apply_payment` inside a `panel-core` module reads like a local import while actually inverting the dependency graph and pulling the `yookassa` SDK into every image. The guard resolves each `panel_core.*` import to its owning distribution and requires that owner to be inside the importer's **declared** dependency closure, read from the `pyproject.toml` files rather than hardcoded. The `yookassa` guard in `tests/test_workspace_layout.py` does **not** cover this: it matches literal `import yookassa` statements and never follows a `panel_core.*` edge. Two exemption sets exist, each with its own rationale in the file: the now-empty `ALLOWED_INVERSIONS` above, and `ROLE_DISPATCH_EXEMPTIONS` — `dispatch.py`'s `PANEL_ROLE` branches import `roles/{sub,botapi,master,worker}` *inside* `create_app()`, so each edge is only traversed on a host that installs that distribution by definition. That one is structural and permanent, and holds only while those imports stay function-level (separately asserted).

Every `app/…` path in the list below is shorthand for `backend/packages/<dist>/src/panel_core/…` — e.g. `app/models.py` is `packages/panel-core/src/panel_core/models.py`, imported as `panel_core.models`; `app/api/billing.py` is `packages/panel-botapi/src/panel_core/api/billing.py`, imported as `panel_core.api.billing`.

**`panel_core` is a namespace package (PEP 420).** Neither it nor its splittable subpackages (`api/`, `services/`, `jobs/`, `roles/`, `xray/`, `data/`) carries an `__init__.py`, which is what lets the eight distributions above ship into the same import root. This is no longer hypothetical: `panel_core.__path__` has **eight** contributions today, and every cut from the original three to today's eight changed **zero** call-site import statements outside the moved modules themselves. Consequences you must not undo (guarded by `tests/test_namespace_packages.py`, `tests/test_workspace_layout.py`, `tests/test_xray_facade.py`, `tests/test_bootstrap.py` — the workspace guard also fails on a module shipped by two distributions at once, and on a workspace member with Python code that no guard scans):
- **Importing `panel_core` runs no code.** What the deleted `__init__.py` files held now lives in explicit modules: `bootstrap.py` (`bootstrap_gevent()` — `gevent.monkey.patch_all()` + `patch_gevent_psycopg()`), `dispatch.py` (`create_app()`, the `PANEL_ROLE` → role-module dispatcher) and `xray/facade.py` (the gateway shims `has_local_xray`, `generate_config_file`, `restart_xray_container`, `stream_xray_logs`, `update_geo_db`, `_api_add_user_grpc`, `_api_remove_user_grpc`). Import them from those modules. Both of the old forms are **already broken today**, not merely fragile under a future split: `from panel_core.xray import generate_config_file` raises `ImportError: cannot import name 'generate_config_file' from 'panel_core.xray' (unknown location)` and `from panel_core import xray` + `xray.generate_config_file` raises `AttributeError`, because a namespace package owns no `__init__.py` and so re-exports nothing. The guard (`tests/test_xray_facade.py`) exists to stop either form being re-introduced — it is not a pre-emptive check against a split that has not happened yet. `xray/facade.py` ships from `panel-core` and dispatches to whichever gateway is bound at runtime; the local implementation it shims, `LocalXrayGateway`, lives in `xray/local.py` and ships from `panel-worker` — the only role with a local Xray to gate.
- **gevent patching is now every entry point's own job.** `run.py` (dev) calls `bootstrap_gevent()` on its first lines; `tests/conftest.py` calls it before importing anything else from `panel_core`. In containers nothing in Python does it — gunicorn's own worker does: `GeventWorker.init_process()` calls `gevent.monkey.patch_all()` before `base.Worker.init_process()` reaches `load_wsgi()`. That holds only while the gunicorn command keeps `-k gevent` and stays **without `--preload`** (with `--preload` the arbiter imports the app in the unpatched master process before forking). `tests/test_compose_gunicorn_gevent.py` guards both conditions across all five gunicorn commands in `docker-compose*.yml` (it was eight until wave 10 deleted the three monolithic files).
- **psycopg is patched on every *role* path** regardless: `build_base_app()` calls `patch_gevent_psycopg()` itself, so all four roles get the gevent wait callback even though `bootstrap_gevent()` was never called in-process (`tests/test_bootstrap.py` parametrises that over all four). The one exception is `sqlite_to_pg.py`, which builds no Flask app and calls neither `bootstrap_gevent()` nor `patch_gevent_psycopg()` — it reaches Postgres as plain blocking psycopg2. That is the right mode for a one-shot CLI migration (there is no gevent hub to block), but it *is* a behaviour change the namespace conversion made: the script used to inherit the patch from the deleted `panel_core/__init__.py`, and nothing replaced that side effect. Do not describe the patch as universal.
- **Package data is reached through `panel_core/resources.py`, never through `__file__`.** `resources.data_file(name)` / `read_data_text(name)` resolve via `importlib.resources.files("panel_core.data")`, which on 3.12 returns a `MultiplexedPath` that searches *every* distribution contributing to the namespace. The `__file__`-relative form is the same defect class as the `instance_path` one and fails the same way: `api/bot_admin.py` did `os.path.join(os.path.dirname(__file__), "..", "data")`, which under a two-distribution **editable** install (production's mode — `uv sync --frozen --no-dev`) resolves into the *api* distribution's tree, where `data/` does not exist. It failed silently — `GET /api/bot/texts/keys` returned HTTP 200 with `{"keys": []}` and the Bot → Texts tab went blank, no error, no log line. `db_migration.py`'s bot-texts seeder had the same shape (`__file__` + `"data"`). A non-editable wheel merges both trees into one `site-packages/panel_core/` and hides all of it, so this only ever breaks in production's install mode. `tests/test_resource_paths.py` rejects any `__file__`-derived path segment naming `..` or a namespace subpackage.
- **`root_path` and `instance_path` are passed to `Flask` explicitly** (`app_base.py`: `Flask("panel_core", root_path=PACKAGE_ROOT, instance_path=INSTANCE_PATH)`). Flask derives `root_path` from the package's `__file__` (a namespace package has none) and `instance_path` via `_find_package_path`, whose namespace branch does a bare `next()` over the search locations and raises `StopIteration` as soon as more than one location contributes — so leaving either to auto-discovery would break the moment the package is actually split. `INSTANCE_PATH` is `sys.prefix/var/panel_core-instance`: `sys.prefix` is unambiguous no matter how many distributions contribute, while any formula derived from the package location is not. Production installs `panel-core` **editable** (`uv sync --frozen --no-dev` in `backend/Dockerfile`), so this changed the value from `/app/packages/panel-core/src/instance` — harmless, because nothing reads `instance_path`. The only way to make it meaningful is a *relative* sqlite `DATABASE_URL` (`sqlite:///panel.db`), which Flask-SQLAlchemy resolves against `app.instance_path`. Nothing reaches that path today. Three of the eight compose files set `DATABASE_URL` at all — `docker-compose.{master,sub,bot}.yml`, each as a pass-through `${DATABASE_URL:?…}` that the compose file itself does not constrain, and `.env.{master,sub,bot}.example` fill all three with a `postgresql+psycopg2://…` URI. `docker-compose.node.yml` deliberately sets none (see the role paragraph above): the worker falls through `db_config.database_uri()` to `sqlite:///` + `app_base.db_path()`, which is **absolute** (`$CWD/db/panel.db`, mounted from `./db_data`) and therefore never consults `instance_path`. So a relative sqlite URI would have to be set by hand, against the only three roles whose compose requires the variable and expects Postgres — it is reachable, but nothing in the repo produces it.

- `app/app_base.py` + `app/dispatch.py` + `app/roles/{master,worker,sub,botapi}.py` — Flask app factories; register blueprints, extensions, ProxyFix, APScheduler jobs per role
- `app/models.py` — SQLAlchemy models (22 total). Core: `Admin`, `Inbound`, `Client`, `Outbound`, `RoutingProfile`, `Balancer`, `SystemSetting`, `TrafficSnapshot`, `DomainStat`, `LinkedPanel`, `FederationConfig`, `UserDevice` (the device ledger, keyed by `telegram_id` — see Device limit). Billing/bot: `Tariff`, `TariffItem`, `UserTariffAccess`, `Payment`, `BotText`, `BotEvent`, `TelegramUser`, `NotificationLog`, `NotificationClaim`, `ProvisionReceipt` (the node-side idempotency ledger — see Panel Federation). **FK enforcement is OFF** — `extensions.py` sets WAL/synchronous/busy_timeout/temp_store but **not** `PRAGMA foreign_keys=ON`, so FK constraints are advisory (deleting a parent leaves dangling child refs rather than cascading/erroring; e.g. `delete_tariff_permanent` can orphan `Client.tariff_id`, though it now refuses outright while the tariff has grants — see Grants). Exception: deleting a `LinkedPanel` (`delete_panel`) or an `Inbound` (`delete_inbound`, local + remote-via-`panel_id`) app-level cascades the matching `TariffItem` rows through `services/tariffs.purge_tariff_items`, which also disables any tariff left with zero items — so a removed panel/inbound can no longer orphan a `TariffItem` and 500 provisioning.
- `app/extensions.py` — Shared Flask extensions (db, migrate, APScheduler, Flask-Limiter, SQLite PRAGMAs)
- `app/utils.py` — JWT helpers + auth decorators: `token_required` (admin JWT only), `bot_service_token_required` (bot service token only), `federation_token_required` (validates federation token from linked panels), `admin_or_federation_token_required` (admin JWT **or** federation token — exactly two, since wave 4a). The latter two support the Panel Federation system. There is no dual admin/bot decorator any more: `admin_or_bot_token_required` and the bot-token branch inside `admin_or_federation_token_required` were both removed once the bot stopped calling the admin API — see Auth below.
- `app/api/`
  - `auth` — login / logout
  - `inbound`, `outbound`, `routing`, `panels`, `federation`, `subscription`, `statistics`, `system` — core panel. Every Xray-facing handler in `system.py` is behind `has_local_xray()`, and since wave 5c all of them except `/api/logs` also take `?panel_id=` **above** that gate (see Xray settings, config and per-user routing are answered by the node that runs Xray)
  - `backup` — `GET /api/backup` + `POST /api/restore`, **registered only by `roles/worker.py`, and since wave 7 shipped only by `panel-worker`**; both copy a SQLite file, which the master (Postgres) does not have. Being unregistered was never the whole story: while the module shipped from `panel-adminapi` it travelled in the master image too, so one `register_blueprint` line stood between the master and handing out its own database (verified: adding it answered 200). `api/federation.py` was the same, and worse — it would have made the master linkable as somebody's node. Both moved. See Auth below
  - `billing` — YooKassa checkout + webhook. The webhook is **unsigned**, so the body is treated only as a trigger: the handler re-fetches the authoritative status from YooKassa (`fetch_remote_status`) before provisioning, so a forged notification does nothing
  - `bot_admin` — admin UI endpoints (tariffs, texts, users, grants, payments, settings) — JWT-protected
  - `bot_service` — endpoints the bot itself calls (runtime-config, texts, users, trial, tariffs, payments) — bot service token only
- `app/services/`
  - `xray.py` — generates Xray JSON config, gRPC user add/remove, traffic stats, log tailing. File lock `/etc/xray/config.lock` serializes concurrent writes
  - `traffic_store.py` — pure SQL layer for traffic storage, usable by roles with **no local Xray**: snapshot upserts (`_ten_min_bucket`, `_upsert_snapshot`, `_upsert_domain_stat`), cleanup (`cleanup_old_domain_stats`, `cleanup_stats_job`), and the admin-surface counter resets (`reset_user_traffic`, `reset_inbound_traffic`, `bulk_delete_users`) that touch Xray only through `XrayGateway`
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
  - `bot_status.py` — the bot's reported version, carried through the **shared** Redis (`SETEX panel:bot:status`, 180s TTL). The writer is `record_bot_version` on **bot-api** (the bot stamps `X-Bot-Version` on its 60s `GET /bot/runtime-config`); the only reader is `GET /api/system/version` on the **master**. It holds **no module-level state** deliberately — a dict here was filled in one container and read as empty in another, which is why System → About showed no bot row at all until wave 5b (§67, same class as `version_check` above). With no shared tier reachable it reports `None`, and the UI hides the row
  - `expiry.py` — `nearest_expiry(values, *, fallback)`: the single fold behind every "when does my access end" a user sees. `None` (a damaged row, §10.5) is ignored, `0` means never and absorbs, otherwise the **nearest** date wins. Three call sites — `provisioning.apply_tariff_for_user`'s reply (which becomes the bot's "access until X"), `bot_service`'s `/users/<id>/state`, and both of `subscription.py`'s (`expiry_at` on the page, `expire=` in the `subscription-userinfo` header a client app reads). `backfill_tariff` deliberately does **not** use it — see Provisioning
- `app/jobs/`
  - `billing.py` — `reset_grant_traffic_cycles` (zeroes a granted user's traffic counters once per tariff period) — ships from `panel-cron`
  - `payments.py` — `poll_pending_payments` (30s webhook fallback), `reconcile_refunds` (1h refund-webhook fallback → `billing.handle_refund` revokes access), `cleanup_old_payments` (24h, cancels stuck pending + publishes notification)
  - `notifications.py` — `cleanup_bot_events`, `replay_undelivered_bot_events` (also registered on the worker role, not just master). There is no `send_expiry_notifications`/`send_traffic_notifications` cron — expiry and traffic warnings are emitted inline from `stats.py`'s `check_limits_and_reset` and `sync_traffic_stats` via `services/notifications.emit_if_new`
  - `panels.py` — ships from `panel-cron`. `poll_linked_panels` (10s health poll of every node), `poll_panel_now(panel_id)` (the same poll for one node, out of band) and `run_refresh_listener(app)`, the greenlet subscribed to the `panel:refresh` channel that calls it

### Frontend (`frontend/packages/`)

`frontend` is an npm workspace (`frontend/package.json`: `workspaces: ["packages/*"]`) of four packages, each with its own `package.json`, `vite.config.ts`, `tsconfig.json`, `index.html`, `tailwind.config.js` and `postcss.config.js` — there is no root-level `index.html`/`vite.config.ts`/`tailwind.config.js`/`postcss.config.js` any more; only `entrypoint.sh` and `nginx.conf.template` stay shared at `frontend/`. Neither `@panel/admin`'s nor `@panel/node`'s nor `@panel/sub-page`'s `package.json` declares a dependency on `@panel/ui-core` — there is no workspace dependency edge at all, only an alias: each package's `tsconfig.json` and `vite.config.ts` map `@ui` → `../ui-core/src` and `@` → the app's own `src` (so a bare `@/pages/Panels` inside `admin` can never resolve inside `ui-core`, and vice versa). That alias is necessary but not sufficient — a relative specifier crosses the same boundary without ever touching it, which is why the import direction is also enforced by a dedicated guard (`backend/tests/test_frontend_import_direction.py`) rather than by the alias or the dependency graph.

- `packages/ui-core/src/` — everything shared by the three apps (56 files, every file under the directory regardless of extension — 34 `.ts`/`.tsx`, `index.css`, plus `fonts.css` and the 20 self-hosted Roboto/Roboto Mono `.woff2` files added in Phase 6 when the Google Fonts CDN link was dropped): `pages/` (`Dashboard`, `Routing`, `Statistics`, `System`, `Login` — the five pages every role has; `Statistics` joined them in wave 4d, `Routing` in 4c-2), `components/inbound/` (`InboundForm`, `UserForm`), `components/ui/` (`Select`, `Modal`, `ConfirmationModal`, `Button`, `Input`, `Switch`, `TagInput`), `components/layout/` (`Layout`, `Sidebar`, `AnimatedBackground`), `components/DisplayConfigLoader.tsx`, `hooks/` (`useLinkedPanels`, `useVersionStatus`), `lib/` (`api.ts` — axios client with auth interceptor; `types.ts` — TS interfaces for every API entity; `protocols.ts` — protocol + stream-settings definitions; `panelRole.ts`/`assertPanelRole.ts` — role gating, see the deploy note below; `panelBase.ts`, `datetime.ts`, `devices.ts`, `routing-validation.ts`, `utils.ts`, `version.ts`), `stores/` (Zustand stores for auth + log state), `index.css`.
- `packages/admin/src/` — admin-only surface (18 files, same counting rule as ui-core above — this one happens to be all `.ts`/`.tsx`): `App.tsx`, `main.tsx` (the entry points), and the master-only pages/components `pages/Panels.tsx` (federation management), `pages/Bot.tsx` (billing UI) plus `components/bot/` (`TariffsTab`, `TariffDrawer`, `TariffsTable`, `TariffRowMenu`, `UsersTab`, `UserDrawer`, `GrantsTab`, `PaymentsTab`, `PaymentStatusBadge`, `TextsTab`, `SettingsTab`, `TrialCard`) and `lib/bot.ts`.
- `packages/node/src/` — **has no page of its own**: just `App.tsx`, `main.tsx`, `vite-env.d.ts` (3 files, same counting rule as the two bullets above). `App.tsx` wires up only the shared `Dashboard`/`Routing`/`Statistics`/`System`/`Login` pages from `ui-core` — every route with its own page component lives in `ui-core` or `admin`, never in `node`. **`Routing` is no longer gated by `hasLocalXray` in either app, and the sidebar's `LOCAL_XRAY_ONLY` filter is gone** (wave 4c-2): the page is now meaningful on a master too, where it edits a *node's* outbounds through a panel picker in its header. Three places hid it before, and missing any one of them leaves it unreachable while the other two look fixed — the sidebar filter plus the `hasLocalXray ? <Routing /> : <Navigate to="/">` in **both** `App.tsx` files. `backend/tests/test_routing_page_reaches_the_nodes.py` pins all three. **Node-only surface therefore arrives as a gated tab inside a shared page, not as a route:** wave 4b's federation card is a `System` tab that ships from `ui-core` and renders only when `isWorker` — three gates in the bundle (the tab entry, its body, and `enabled: isWorker` on the `GET /api/federation/config` query, without which a master would 404 on every visit to System) plus a fourth in the backend, since `roles/master.py` registers no `federation` blueprint. The node's route count went from four to **five** in wave 4d, and for the mirror-image reason: `Statistics` was classified master-only by phase 3e and the classification was inverted — the master's `traffic_snapshot`/`domain_stat` have had no writer since phase 3b, while a node's are the only full ones in the deployment. The page moved `packages/admin` → `ui-core`, gained the same node picker `Routing` has, and `/statistics` left the sidebar's `WORKER_HIDDEN` set. The three gates are the same three: the sidebar filter, the route in `admin/App.tsx`, and the *absence* of a route in `node/App.tsx`. `backend/tests/test_statistics_page_reaches_the_nodes.py` pins all three, `backend/tests/test_federation_card_is_node_only.py` pins the federation card.

  **Wave 5c added no route at all — it un-gated an existing page, and that is a different failure mode.** `System.tsx` decided the whole Xray surface with `hasLocalXray`, which is **false on the master**, repeated in seven places; the capability appears only when every one of them comes off, and any one left behind leaves a screen that looks fixed. Six came off (the Core tab entry, the `enabled:` of the settings query, the Core tab body, the three maintenance buttons, the two confirmation modals, the config modal) and **one stays** — the log panel, because `/api/logs` was left out of the wave. The page grew a node picker that scopes **the Core and Maintenance tabs only** (customer decision): Security is this panel's own admin password and About is its own versions, and a picker over those would put a new lie where the wave removed one. `Dashboard.tsx` had two more of the same shape — the Route button and its modal — and one worse: `routeOptions` was built from a page-level `GET /outbounds` with `enabled: hasLocalXray`, so even un-gated the dropdown would have offered nothing on a master. The lists are now fetched per inbound's `panel_id`, lazily, when the modal opens. `backend/tests/test_system_page_reaches_the_nodes.py` pins every gate individually.
- `packages/sub-page/src/` — the subscription page a user opens in a browser, and the only package that is **not** an admin surface (18 files, same counting rule as the bullets above): `App.tsx`, `main.tsx`, `vite-env.d.ts`, `components/` (`Header`, `Hero`, `Summary`, `QrPanel`, `AppButtons`, `Nodes`, `Footer`, `Loading`, `ErrorState`), `hooks/useSubInfo.ts`, `lib/` (`deeplinks.ts`, `format.ts`, `i18n.ts`, `types.ts`), `index.css`. It has no router, no axios client and no auth store — it reads one endpoint, `GET /api/sub/u/<token>/info`. Three things set it apart from the two admin apps: it ships **no** `assertPanelRole()` call and reads no `panel-role` meta tag (it is served by Flask out of `panel-sub`, not by Nginx, and there is no role to get wrong); it carries **its own `index.css`** rather than importing `ui-core`'s, because that one applies `overflow-hidden` to `body` for the fixed-chrome admin layout and would make a scrolling page unreadable on a phone; and it is built into the `panel-sub` backend image by `backend/Dockerfile.sub` rather than into an Nginx image of its own. It still looks like the same product, but only one of the two reasons is actual sharing: `ui-core/src/fonts.css` (self-hosted Roboto) is imported by all three packages and is sub-page's **only** edge into `ui-core`, while the Tailwind theme is a *duplicated copy* — `ui-core` has no `tailwind.config.js`, each package declares its own palette, and sub-page's config does not even scan `../ui-core/src`. That distinction decides the release fan-out — see point 3 of the Phase 3d deploy note.

Each of the two admin apps bakes its role at build time (`vite.config.ts`'s `define: { __EXPECTED_PANEL_ROLE__ }`) and asserts it at runtime against the `<meta name="panel-role">` tag that `entrypoint.sh` rewrites in `index.html` at container start (read by `lib/panelRole.ts`'s `readInjectedPanelRole()`). A meta tag, not an inline script: the reverse proxy's CSP sets `script-src 'self'`, which blocks inline `<script>` outright — see the deploy note below.

### Caddy (`caddy/`)
- `routes.yaml` — declarative per-SNI routes (the only hand-edited Caddy config). Fields: `match` (SNI host, `${ENV}` interpolated), `upstream` (`host:port`), `tls` (terminate vs raw passthrough), `only_paths` (path-prefix allowlist → 404, implies `tls`, and **also `${ENV}`-interpolated** — the bot host's webhook lives under a secret segment), `strip_prefix` (removed before proxying, so the upstream sees the path it registered). A route whose `match` is empty after interpolation is **dropped** (so an empty `SUB_DOMAIN` drops the subscription route). Interpolated path fields are collapsed (`//` → `/`), so an unset secret degrades to the plain path instead of a `//…` nobody can reach.
- `caddygen/` — small Go program that reads `routes.yaml` + env and emits Caddy's **native JSON** (entrypoint runs `caddygen → caddy validate → caddy run`). See "TLS, Caddy & certificates" below.

### Telegram Bot (`tg_bot/`)
- `main.py` — aiogram entry: bootstraps `runtime_config` → builds `Bot` → starts polling + bot-events consumer; on runtime change (token/proxy hot-swap) it stops polling, closes the old aiohttp session, builds a new `Bot`, and restarts polling **without** restarting the consumer (consumer holds a Bot-accessor closure, not a fixed ref)
- `runtime_config.py` — polls `GET /api/bot/runtime-config` every 60s; emits a change event when bot_token / telegram_proxy_url shift
- `backend_client.py` — thin async HTTP wrapper around `/bot-service/*` endpoints
- `bot_events_consumer.py` — subscribes to Redis `bot:events`, dispatches `payment_*` / `access_*` / `expiry_notification` / `traffic_notification` / `texts_changed` / `user_*` events
- `i18n.py` — `BotText` cache, `t(key, lang, **kwargs)` formatter (missing key → `⟨key⟩`, falling back to the other language first)
- `middleware.py` — `LangMiddleware`: per-user language lookup, cache, invalidation on `user_language_changed`
- `handlers/user.py`, `handlers/catalog.py` — message + callback handlers. There is **no `handlers/admin.py`**: wave 4a removed the bot's whole admin surface (backup, restore, restart, server listing). Fleet management lives in the master panel only. `catalog.start_checkout` **answers the Telegram callback before calling bot-api** and finishes in an `asyncio` task (wave 5a): `create_checkout` waits on YooKassa for up to ~16s (8s × 2 attempts), which used to be spent with the button spinning. It clears the catalogue keyboard on the way out and keeps a per-user in-flight set, so one press stays one `Payment` row and one YooKassa idempotence key; the same message later becomes the pay screen or an error. If the bot restarts inside that window the user is left on a keyboard-less catalogue — pressing Tariffs again is the recovery, and any orphan `pending` payment is cancelled by `cleanup_old_payments` after 24h
- `keyboards.py`, `states.py`, `utils.py` — UI builders, FSM states, helpers
- `config.py` — env validation: `BACKEND_API_URL`, `BOT_SERVICE_TOKEN`, `BOT_LOG_LEVEL`

The bot is **backend-client** (not standalone) — it has no local SQLite. All state (users, languages, notifications, payments) lives in the panel's `panel.db`. **One Telegram token may only long-poll once**, so run the `bot` service against a single master; never start a second poller with the same token (it would 409 the first).

**Every user-facing screen is built from one response — `GET /bot-service/users/<id>/state`.** That response carries, per client, `up`/`down`/`limit_bytes`/`expiry_time`/`enable`/`inbound_label` (and `panel_name` for a client on a node), plus a `links` array of ready share links, plus the account's `sub_url` and aggregate `expires_at_ms`. So "Statistics" needs no second call, "Keys" prints `record["links"]`, and the QR button encodes that key's own link (the subscription screen's QR encodes `sub_url`). Do **not** add a path from the bot to the admin API to fill any of this in: bot-api serves 15 routes and none of the admin ones, which is precisely how phase 3c-2 broke all three screens at once — `tg_bot/api_service.py` kept calling `/api/inbounds`, `/api/panels`, `/api/stats/system`, `/api/sub/<uuid>`, every one 404'd, and the bot rendered the 404s as "no keys" / "unavailable" / "No active key found". `tg_bot/tests/test_no_admin_surface.py` fails on any module that reaches for one of those paths.

**`links` is empty for a client with no `panel_id`, and that is correct.** bot-api can build a link only for a client it sees in a node snapshot, where the node's own hostname comes from `LinkedPanel.url`. For a local `Client` row in bot-api's own Postgres there is no right hostname to use — the bot host is not a node — so the screen says "no link" rather than handing out a confidently wrong address. In a split topology such rows do not exist anyway (`_require_local_xray` has blocked creating them since phase 3b).

## Key Concepts

### Xray integration
`xray.py` both writes the full JSON config to `/etc/xray/config.json` and manages live users via the Xray Handler/Stats gRPC API. Config regeneration and Xray restart happen together when inbounds/outbounds change. The file lock `/etc/xray/config.lock` serializes concurrent writers (request handlers + the scheduler). gRPC requires gevent-compatible setup: `grpc_gevent.init_gevent()` runs at app startup before any gRPC import; current pin `grpcio==1.66.2` on Python 3.12.

### TLS, Caddy & certificates
`caddy/caddygen/` generates Caddy's native JSON from `caddy/routes.yaml` at container start (`caddy validate` runs before `caddy run`, so a bad config fails fast). The generated config uses the **caddy-l4** layer4 app listening on `:443`, routing by **TLS SNI**:
- `PROXY_DOMAIN` (decoy) → raw-TCP passthrough with PROXY-protocol to `xray:443`, so Xray sees the real TLS/REALITY handshake (masquerade).
- `PANEL_DOMAIN` / `SUB_DOMAIN` → TLS terminated at Caddy, PROXY-protocol'd to a per-route loopback HTTP server (security headers + CSP, optional path filter) → `frontend:80` / `backend:5000`.
- caddygen also emits a plain `:80` server that 308-redirects everything to https.

**Caddy issues and renews every certificate itself over ACME (wave 11).** `tls.automation.policies` carries the subjects and the issuer; there is no `load_files` entry and no `./certs` mount anywhere any more. Certificates live in the `caddy_data` volume, which every host already had — **do not delete it on upgrade**, or Caddy re-issues from scratch and Let's Encrypt allows only 5 identical certificates per week.

Four things about how the subjects are chosen, each load-bearing:

- **Only routes with `tls: true` become ACME subjects.** The decoy is a raw passthrough whose SNI is somebody else's domain (`www.google.com` in every example), so requesting it cannot succeed, and LE counts *failed* validations against the account — a node would spend that budget on every restart and then fail to get the certificate it actually needs, for a reason nothing connects to the decoy. `tests/…/generate_test.go` pins the exclusion.
- **A local hostname gets Caddy's `internal` issuer instead** (`localhost`, `*.localhost`, `*.local`, a bare IP, any name without a dot). A public CA cannot validate those, so a local deployment would otherwise fail to start behind a wall of ACME errors. This is also what replaced the deleted `generate_local_cert.sh`.
- **The `:80` server keeps `automatic_https: {disable: true}`, and that does not block the challenge.** Caddy solves HTTP-01 in `Server.ServeHTTP`, which calls `tlsApp.HandleHTTPChallenge` **before** any user-defined route and with no dependence on the port or on `automatic_https` (verified against `modules/caddyhttp/server.go` and `modules/caddytls/tls.go`). So the catch-all 308 does not shadow it. What matters is only that a server still listens on `:80` — delete that and nothing in the process can answer the challenge.
- **TLS-ALPN-01 is unavailable by construction**: layer4 owns `:443`. HTTP-01 over `:80` is the only mechanism, so **`:80` must be reachable from the internet** on all four hosts, and the domain must resolve to that box. A cloud firewall closing 80 breaks issuance with no other path.

`ACME_EMAIL` (LE's expiry warnings) and `ACME_CA` (point at the LE **staging** directory while rehearsing a deploy, so a debugging session does not burn the weekly limit) are optional and passed to the `caddy` service on all four hosts.

**The data tier is outside all of this** — no Caddy, ports bound to a private address, and Postgres/Redis sharing one pair from `./pg_certs`. It is covered by its own long-lived CA, not by ACME.

System → About no longer shows a certificate line: Caddy owns the expiry now and keeps the file where the backend cannot read it, so the only reading left would be "not mounted" forever on a healthy host (wave 11 removed the card added in wave 6).

### Traffic enforcement
`stats.py` polls per-user up/down via Xray gRPC every 10s, writes to `Client.up`/`down` and upserts hourly `TrafficSnapshot` rows. `check_limits` (60s) removes users that exceed limit or expiry. Monthly resets (per-client `reset_day`) zero the counters **and** delete that client's `traffic_*` `NotificationLog` rows so the next cycle's warnings can fire.

### Device limit

**One ledger, keyed by the Telegram account, enforced on exactly one role.** `UserDevice` is unique on `(telegram_id, hwid)`; `services/device_tracking.user_device_gate(telegram_id, headers)` registers or refreshes a row on every config request and answers `limit` once the account is over `device_limit_per_user` (both that and `device_limit_enabled` are `SystemSetting` rows). The gate runs on the **sub** role, because since wave 3b that is the only role serving subscriptions — which also makes sub a **writer** of the shared Postgres, so a read-only credential there breaks the hot path rather than degrading it.

Two things about the grain are deliberate and easy to undo by accident:
- **Nothing joins through `Client`.** The predecessor counted `ClientDevice → Client` on `Client.telegram_id`, so the budget was whatever the serving role's database happened to hold. On a node that meant its own clients only — a user with keys on three nodes had three independent budgets — and on sub it would have meant *zero*, since no `Client` row for a node-issued client exists in Postgres at all (`Client` has no `panel_id` and the master mirrors none). Both failures were silent. The ledger therefore stores `telegram_id` and nothing else identifying.
- **A client with no `telegram_id` has no device tracking.** Per-client and per-inbound limits are no longer enforced anywhere, and since wave 4d they are no longer *offered* either: the `Client.device_limit` / `Inbound.device_limit` **columns** remain (dropping a column from an existing table is the one thing the Postgres migration path cannot do — see Database migrations), but nothing accepts them on input, `Client.to_dict()` and the federation snapshot no longer return them, both forms lost the field, and the Dashboard's device chip lost its denominator — it now shows the real global count and no cap. Editing them used to succeed and change nothing. The only gate is the global one, and it needs a Telegram account to count against. Admin-created keys are outside it. `backend/tests/test_device_limit_stops_offering_itself.py` holds both halves — that the columns survive and that nothing offers them.

The admin surface reads the same ledger: `GET /users/<telegram_id>/devices` and `DELETE /users/<telegram_id>/devices/<id>` (admin JWT), and `device_count` per client in `GET /api/inbounds` is that account's count — the same number the gate sees and the same number the subscription page shows. Node snapshots carry no `device_count` any more; a node cannot know it, since the ledger lives in a Postgres it never reaches.

### Background scheduler jobs

| Job | Interval | What it does |
|---|---|---|
| `sync_traffic` | 10s | Per-user up/down from Xray gRPC; upserts `TrafficSnapshot` via raw SQL `ON CONFLICT DO UPDATE`; emits `traffic_notification` inline at 80%/95%/exhausted (dedup via `NotificationLog`) |
| `check_limits` | 60s | Removes expired/over-limit users; emits `expiry_notification` inline at 3d/1d/1h/expired (dedup via `NotificationLog`) |
| `parse_logs` | 15s | Tails Xray access logs into `DomainStat` (skips bare IPs) |
| `cleanup_stats` | 24h | Runs on the **worker role only** — the master registers no scheduler at all (`no scheduled jobs on this role`), and its `DomainStat` has had no writer since phase 3b. Deletes `DomainStat` rows > 90d |
| `poll_linked_panels` | 10s | Runs on the **cron service only**. Pings each enabled `LinkedPanel`; fresh `snapshot`/`status`/`last_poll` go to the **shared** Redis every poll, the Postgres row is written **only on status/error change** (the panels API overlays the Redis values). A `panel:refresh` message polls one panel out of band, without waiting for the next tick |
| `reset_grant_traffic_cycles` | 15m | Runs on the **cron service only**. Zeroes `up`/`down` on the nodes for grants whose `next_renewal_at` has arrived and moves that date one tariff period out. It **provisions nothing and touches no expiry** — a granted access carries its own `access_until` and is not renewed by anybody. A tariff with no traffic limit gets no date at all, and archiving or disabling a tariff no longer pauses its grants |
| `poll_pending_payments` | 30s | Runs on the **`bot` (bot-api) role only** — not the master; webhook fallback, reconciles pending YooKassa payments older than 30s, younger than 24h. Since wave 6 it first calls `release_stranded_claims()`, which returns a payment left in `processing` by a dead process to `pending` — see Bot billing flow |
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
- `admin_or_federation_token_required` — accepts admin JWT **or** federation token, and only those two. **Thirty-nine** handlers: thirteen in `inbound.py` (user/inbound CRUD, the `/users/bulk-*` + `/users/reset-traffic` batch endpoints, and — since wave 4c-2 — `/inbounds/<tag>/reset-traffic`, the last one that could not be routed by `panel_id`), **six** in `system.py` (`/api/restart` and `/api/stats/system`, plus — since wave 5c — `GET`/`PUT /api/system/settings`, `GET /api/config` and `POST /api/system/update-geo`), `POST /api/user/routing` in `auth.py` (wave 5c), `GET /api/backup` + `POST /api/restore` in the node-only `backup.py` (wave 4c-1), and — since wave 4c-2 — twelve more in `outbound.py`/`routing.py`: outbound CRUD, balancer CRUD and routing-profile CRUD, so the master can manage a node's whole egress and routing layer, and — since wave 4d — the five in `statistics.py`, so it can read a node's traffic figures. **Two handlers stay `token_required` on purpose**: `GET /outbounds/health`, because a reachability probe is only meaningful from the box the traffic leaves through, and `GET /api/logs`, because it is a stream while `FederationClient._call_reporting` ends in `.json()` — see Xray settings, config and per-user routing below.

All three decorators stamp `g.auth_via` (`"admin"` / `"federation"`) on the way through, which is how `backup.py` can log *which credential* took a node's database and not merely that someone did. A federated backup or restore leaves a WARNING on the node; the node's own admin leaves an INFO.

**`GET /api/backup` and `POST /api/restore` live in their own blueprint that only `roles/worker.py` registers, so the master answers 404 on both.** They used to sit in `system.py` under `token_required` — admin JWT only — while `panels.py` proxied to them with nothing but an `X-Federation-Token`, so backing a node up from the master answered 401 from the first release and no path to it existed. Both halves are gone: the routes take the federation token now, and they are node-only, because both copy a SQLite file and the master keeps its data in Postgres. There, `/api/backup` answered `404 "DB not found"` (which reads as *your database is gone*) and `/api/restore` tore down the live Postgres pool, wrote the upload where nothing reads it, restarted the worker and answered `{"status": "restored"}` — a disaster-recovery path confirming a recovery that never happened. A cheap `is_postgres()` refusal (409, naming `pg-backup`) also guards the handlers themselves, because `docker-compose.node.yml` merely omits `DATABASE_URL` rather than forbidding it. **The master's own database is backed up by the `pg-backup` container in `docker-compose.postgres.yml`, never through the panel** — System → Maintenance says so where the buttons used to be.

**The bot service token opens `/bot-service/*` and `/billing/checkout` and nothing else — that is a wave-4a change, and it is bigger than it reads.** Two separate paths used to accept it on the admin API. The explicit one was `admin_or_bot_token_required` on seven endpoints (`GET /api/inbounds` plus all six in `panels.py`). The quiet one was a third branch **inside** `admin_or_federation_token_required` — `if _check_bot_service_token(token)` — which put another 14 endpoints behind the same token: inbound and user CRUD, all six batch operations, `/api/restart` and `/api/stats/system`. So a leaked bot token could create and delete any user and any inbound, on the master and through it on any node by `panel_id`. Both are gone, along with `_check_bot_service_token` itself. The branch was unreachable in practice only because `tg_bot/api_service.py` was broken — restoring the bot by handing it admin endpoints, the obvious-looking fix, would have reopened it. `tests/test_bot_token_scope.py` builds the master role's app and asserts 401 on all 21, plus both positive paths.

JWT tokens (2h expiry) carry a `pwdv` (password version) field tied to `Admin.password_changed_at` — changing the admin password instantly invalidates all existing tokens. The axios interceptor in `lib/api.ts` auto-logs out on any 401.

**Every long-lived credential can now be replaced, and each one breaks something different while you do it.** Wave 6 closed the last two:

| Credential | Replace it from | What happens the moment you do |
|---|---|---|
| admin JWT | change the admin password | every existing token dies (`pwdv`) |
| `federation_token` | the **node's** System → Link, *Revoke access & issue token*, then Panels → *Relink* on the master (wave 4b) | the node is unreachable to the master entirely until relinked; a purchase in that window stays `pending` |
| `bot_service_token` | Bot → Settings → *Regenerate token* | **the bot stops working until `BOT_SERVICE_TOKEN` is updated in the bot host's `.env` and the bot is restarted.** Deliberately no grace period — that would leave a leaked token valid for the length of it, the trade-off wave 4b rejected for the federation token. Since wave 6 the bot logs this at **ERROR**, once per outage, naming the variable; before, both of its loops logged 401 at INFO beside ordinary network hiccups, so a permanently disconnected bot looked like a slow backend |
| `sub_token` | Bot → Users → the user's card → *Reset link* (wave 6) | the old subscription URL dies immediately — every `/api/sub/*` route resolves the account by token in the database *before* any cache — and the bot messages the user the new one. Their keys, expiry and access are untouched: this rotates an address, not a subscription |
| `EGRESS_INTERNAL_TOKEN` | env on both containers | restart of the stack |

One ordering trap in the `sub_token` path worth knowing before touching it: `sub_cache.invalidate_user_aggregate` looks the token up in the row, so it must run **before** the value is replaced. Afterwards it would clear the keys of the *new* token and leave the old ones serving the leaked link until they expire.

### Grants

**A grant carries its own term, and the tariff period no longer decides access.** `UserTariffAccess.billing` has **two** values, and they differ by meaning rather than by which machine keeps them alive:

- `free` — **issued access**. `access_until` is the whole of it: `NULL` means it never expires, a date means it ends there. The date is editable (`PATCH /bot/users/<tg>/grants/<tariff>`).
- `paid` — **the right to buy** a private tariff. Provisions nothing; the user still pays.

`gift` is gone. It differed from `free` only in that the cron renewed one and left the other to lapse, so "a gift for one period" is now "a grant with a date", and `access_granted_once` lost its only publisher (its bot branch and its text went with it, bot texts 18 → 19).

Three consequences, each of which is the point rather than a side effect:

- **An open-ended grant assigns `expiry_time = 0` on the node, and every layer already reads 0 as "never".** `evaluate_expiry` returns `None` for it and `check_limits_and_reset` only disables a key whose expiry is above 0, so the holder receives no expiry warning and is never cut off — with no code anywhere switching notifications off. The predecessor issued a dated key and had `auto_renew_free_users` re-provision it **after** it had lapsed, which cost the holder a "your access ends in 3 days" every cycle, up to 15 minutes offline per cycle (the node disables on a 60s tick, the cron renewed on a 900s one), and access entirely if the cron host stayed down longer than a tariff period.
- **The tariff period now drives only the traffic counter.** `next_renewal_at` means *when to zero `up`/`down`*, nothing else, and `reset_grant_traffic_cycles` is the only thing that reads it. Being late for that costs nobody anything, which is what made the old 15-minute lateness harmless. A tariff with no traffic limit gets **no** date at all — there is no counter to zero, so the cron never reaches that holder's nodes.
- **The grant wins over the key only when somebody acts on the grant.** Issue, edit the term, revoke — the keys are rewritten to match. Between those the panel does not touch a key, so an admin's manual extension in the Dashboard survives. This is deliberate: reconciling on a timer would remove hand-editing entirely, and prod runs on that freedom today (a paused grant beside a key extended by hand for months).

**Two guards fall out of the topology rather than from taste.** Every tariff in this deployment routes through the same inbounds, so a user has exactly one key per node and any purchase rewrites its date and its traffic limit:

- **A holder of open-ended access is offered nothing to buy.** The catalogue is empty for them, `create_checkout` refuses with `open_ended_access` before any `Payment` row exists, and the trial refuses **without burning the single attempt**. Otherwise somebody with an unlimited permanent grant could buy a 30-day 300 GB tariff and pay to be worse off. The accepted cost is that an upgrade is closed for them too; the admin edits the grant instead.
- **A tariff with holders cannot be permanently deleted** (409, naming the count). The grant cascades away with the tariff row while the key stays on the node — survivable only while such a key expired on its own, which an open-ended one never does. Payment history already blocks deletion for the same class of reason.

**Existing installations are converted once**, by `jobs/grant_backfill.backfill_open_ended_grants` at cron start-up, guarded by the `grants_open_ended_backfill` `SystemSetting`. A `free` grant with a renewal date was being renewed forever, so it becomes open-ended and its key loses its date; its renewal date is already the right first traffic-reset date and is left alone. A grant with **no** renewal date is paused — the cron stopped renewing it when its tariff was archived — and is not resurrected. The flag is written only if every key was rewritten: recording success while a node was unreachable would leave that holder dated forever with nothing left to retry it.

### Bot billing flow

0. The bot's catalogue (`GET /bot-service/tariffs`) only lists tariffs this role can actually deliver — see the deliverability gate below
1. Bot → `POST /api/billing/checkout` with `{telegram_id, tariff_id, lang}` (bot service token). The bot **answers the Telegram callback before this call**, takes the catalogue keyboard away and builds the invoice in a background task (`tg_bot/handlers/catalog.py`), because step 2 can take ~16 seconds; the same message then becomes the pay screen or an error
2. `services/billing.create_checkout` validates the tariff, creates a `Payment` row (status='pending', placeholder yookassa_id), calls `yookassa.Payment.create` with a `gevent.with_timeout(8s)` + 1 retry on the same idempotence key, then persists `yookassa_id` + `confirmation_url`
3. Bot opens the YooKassa URL in the user's Telegram chat
4. User pays → YooKassa POSTs `https://<BOT_DOMAIN>/<BOT_WEBHOOK_PATH>/api/billing/yookassa/webhook`; Caddy strips the secret segment, so Flask still sees `/api/billing/yookassa/webhook`. The webhook is **unsigned**, so the body is only a trigger — the handler re-fetches the authoritative status via `billing.fetch_remote_status(payment)`; a forged notification re-validates to nothing. (There is no IP whitelist — re-validation replaced it.) **`BOT_WEBHOOK_PATH` is about traffic, not authenticity**: it keeps the one address that matters on the one host that confirms payments from being guessable off the product name, and everything else on that domain answers 404. Losing the webhook entirely is survivable — `poll_pending_payments` confirms within 30s — so this is a latency-vs-exposure trade, not a security boundary.
5. On a confirmed `succeeded` status → `services/billing.apply_payment(payment)`:
   - Idempotency fast-path: `if payment.status == 'succeeded': return`
   - **Atomic claim**: `UPDATE payment SET status='processing' WHERE id=:id AND status='pending'`; if rowcount=0, the poll cron already grabbed it — return
   - Re-validate tariff (still purchasable, items not removed, private+no-grant → fail)
   - `provisioning.apply_tariff_for_user(..., operation_id=f"pay:{payment.id}")` → extends or creates a `Client` per `TariffItem`
   - Sets `status='succeeded'`, publishes `payment_succeeded` to `bot:events` with the `expires_at_ms` **the nodes reported**, which is what the user is shown
   - On provisioning exception, releases claim back to `pending` so the poll cron retries

`poll_pending_payments` (30s) is the fallback when the webhook never arrived; it targets payments aged 30s–24h and runs the same `apply_payment`.

**A payment stranded in `processing` is recovered in minutes, and the claim above must never be widened to do it.** If the process dies between the claim and the end of provisioning, the row is left in a status nothing on the paid path looks for — the poll takes `pending`, and a re-entering webhook returns at `rowcount == 0`. Widening the claim to `IN ('pending','processing')` closes that in one line and reopens the double-grant it exists to prevent, so recovery is a separate branch: `release_stranded_claims()` (called first by `poll_pending_payments`) puts the row back to `pending` and lets the ordinary path have it. `cleanup_old_payments` keeps its own release as the backstop for when the poll itself is not running — its floor was a day and a half, which is what §23's "never recovered" had already decayed into by the time it was fixed. **How long a claim has been held is measured in the process, not in the row**: `Payment` has no `updated_at` — when wave 6 was written, adding a column to an existing table was what the Postgres path could not do (wave 9 lifted that; see Database migrations) — and `created_at` is the wrong clock because the poll reaches back 24 hours and routinely claims payments long after they were created. So the job remembers which ids it has already seen in `processing`; losing that map *is* the event being recovered from, and it costs one extra cycle rather than a wrong release.

**A tariff this role cannot deliver is refused before an invoice exists (wave 5a).** `services/tariff_delivery.is_deliverable(tariff)` — shipped by `panel-botapi`, so it costs one image — answers false for a tariff with **no items at all**, and for one with **any** item whose `panel_id` is `NULL` on a role with no local Xray (`has_local_xray()`, the same predicate `_require_local_xray` uses). It is wired into three places, and all three matter: `_ensure_tariff_available` (so `create_checkout` refuses with `tariff_not_available` **before** the `Payment` row is written, and `apply_payment`'s revalidation refuses before provisioning), the bot catalogue (such a tariff is not listed), and the trial (`_deliverable_trial_tariff`, so `trial_available` reports false and the trial is never *claimed* for a tariff that cannot be granted). The check is **per item, not per tariff**: a tariff with two node items and one orphan is refused whole, and `tests/test_undeliverable_tariff_stops_before_the_money.py` asserts exactly that mixed case, because a tariff-level check passes it. Reachability is installations from the monolith era — the master has refused to save such an item since phase 3b (`bot_admin.py:163`) and the start-up audit only warns (`app_base.audit_tariff_items_without_panel_id`), since no correct `panel_id` can be guessed. `apply_payment` keeps its `LocalXrayUnavailable` → `_fail_payment("provisioning_impossible")` branch as a backstop; it should now be unreachable, and it is cheap insurance rather than dead code.

**Two admin-side halves of the same defect closed in wave 5b, both in `panel-master`.** `_validate_tariff_payload` now demands **at least one item**: `items: []` used to pass, because phase 3b made `panel_id` mandatory *per item* and the loop simply did not run — such a tariff saved fine and the admin's only signal was that it never reached a user (the drawer already refused it client-side, so the backend was catching up, not changing the UI). And `create_grant` wraps `apply_tariff_for_user` in a `LocalXrayUnavailable` handler that rolls back and answers **400 naming the tariff and the orphaned inbound tags**; it used to let the `RuntimeError` reach the generic handler and hand the one person who could fix it an "Internal server error". The rollback is load-bearing — the `UserTariffAccess` row is added to the session before provisioning runs, so a refusal without it would record a grant nobody issued. The cron host is outside this by construction now: `reset_grant_traffic_cycles` provisions nothing, so there is no grant path there left to refuse.

**That retry is why `operation_id` is `pay:<payment_id>` and not a fresh value per attempt.** A multi-node tariff whose second node is down raises after the first node has already been extended, and nothing rolls it back; the payment goes back to `pending` and the cron re-runs the *whole* grant every 30 seconds for up to 24 hours. Before this contract that was harmless — the node assigned an absolute date, so a repeat was idempotent by accident. Now the node adds, and the only thing standing between a stuck payment and a user with several years of access is that every retry carries the same key. Keep the key derived from the payment, never from the attempt.

### Provisioning (`services/provisioning.py`)

`apply_tariff_for_user(telegram_id, tariff, *, source, operation_id)` is the **single gateway** for every grant path (admin grant, trial, paid webhook, backfill). `operation_id` is mandatory — it is the idempotency key that travels to every node (see Panel Federation for what it is per entry point and why). For each `TariffItem`:
- If `item.panel_id` is set → `proxy_provision` to that linked panel with **`period_ms`, never a computed expiry** — the node adds the period to whatever the user still had. This role cannot compute that date: node-issued clients have no `Client` row here, so any expiry it derives is wrong by exactly the remainder it cannot see. That was the bug (a 10-day remainder plus a 30-day purchase yielded 30 days, and the ten paid days vanished at checkout).
- Else if a `Client` already exists for the same (telegram_id, inbound_tag): extend it — bump `expiry_time`, reset `up/down/last_reset_time`, refresh `limit_bytes`, set `enable=True`, clear `traffic_*` `NotificationLog` rows (so the new cycle's warnings can fire).
- Otherwise create a new `Client` with a unique email (`tg<id>_<inbound_tag>` or `_<hex6>` on collision).

**`expiry_time == 0` means "never expires" and is preserved on both branches.** Buying a period on top of unlimited access refreshes the traffic limit and `enable` but leaves the expiry at `0`; adding a period would silently demote the user to a 30-day plan. `NULL` is *not* the same value — it means a damaged row (see the reply check in Panel Federation) and is counted from `now`, so a corrupted client does not become permanent.

**The returned `expires_at_ms` comes back from the nodes**, not from this role's own arithmetic: `apply_payment` puts it into the `payment_succeeded` event and the bot shows it to the user, so computing it locally would report 30 days while the node wrote 40. Several nodes yield several dates; `services/expiry.nearest_expiry` picks one — **`0` absorbs everything, `None` is ignored, otherwise the nearest date wins** (a plain `max()`/`min()` is wrong precisely because unlimited sorts below every date, and NULL is a damaged row rather than "never").

**That function is the *display* fold, and `_collect_tariff_holders` deliberately keeps its own.** Since wave 5b every surface that shows a user one date goes through `nearest_expiry`: this reply, `/bot-service/users/<id>/state`, and both of the subscription role's numbers. Before it the bot took the latest and the subscription page and the `subscription-userinfo` header took the earliest, so one account read "30 days" in Telegram and "3 days" in the client app (§62). `backfill_tariff` is not a display path — it decides what expiry to *write* on a node the user has no key on yet, and there the generous fold (0 absorbs, else `max`) is the right one; unifying the two would silently shorten a backfilled grant.

Every call also clears that user's `NotificationClaim` rows for the tariff (`clear_notification_claims`), so the next expiry/traffic cycle can warn again after a renewal instead of staying suppressed by a stale cross-node claim.

Single `_sync_after_provision` call after the loop: regenerates Xray config (or gRPC-patches for vless/vmess fast-path), restarts container if needed, and invalidates the Redis sub-cache. `backfill_tariff` idempotently ensures every active holder has a key on every tariff inbound (local + remote) without touching existing keys — and it is the one caller that legitimately sends `expiry_ms` rather than `period_ms`.

### Panel Federation

A master panel manages remote *linked panels*. `LinkedPanel` rows store URL + a `federation_token`; `FederationConfig` is a singleton on the child storing the master's credentials. The master proxies user/inbound CRUD **and, since wave 4c-2, the node's whole network layer** — outbounds, balancers and routing profiles — to linked panels via `services/panel_proxy.py` (`FederationClient`). `TariffItem.panel_id` optionally routes a tariff item to a specific linked panel — provisioning then creates the user there instead of locally. `poll_linked_panels` (10s) health-polls each panel — from the **cron service**, which since wave 2 is the single writer of both `LinkedPanel.status` in Postgres and the `panel:<id>:*` keys in the shared Redis. The **thirty-seven** `proxy_*` operations no longer fetch a snapshot themselves; the **twenty-seven mutating** ones publish the panel id on `panel:refresh` and return, and the cron service polls that panel out of band (`_nudge_panel_refresh`). The **ten reads** deliberately do **not** nudge — three from wave 4c-2 (`proxy_list_outbounds`, `proxy_list_balancers`, `proxy_list_routing_profiles`), five from 4d (`proxy_stats_*`) and two from 5c (`proxy_get_system_settings`, `proxy_get_xray_config`): they change nothing on the node, and they run on every load of a page, so nudging would have the cron poll that node out of band each time an admin looks at a list. The rule is exactly that — *mutating nudges, reading does not* — and it is worth keeping in one sentence: wave 5c's `proxy_update_geo` and `proxy_restart_xray` change nothing the master **caches** either, and they still nudge, because a rule with a second clause is a rule nobody applies correctly at 2 a.m. **Never `DEL` the snapshot key instead:** for the sub host a missing key does not mean "stale", it means "this panel has no remote clients", so it skips the panel entirely and a user who has just paid opens the link to a subscription with no node servers in it. Subscription links (`api/subscription.py`) can merge entries from linked panels visible to the requesting client (Redis-cached). Inbound CRUD endpoints accept admin JWT **and** federation tokens (`admin_or_federation_token_required`) so children can proxy operations back through the master.

**Linking a node and revoking its token are the same endpoint, and the master never issues either.** `POST /api/federation/link-token` (admin JWT, node only) mints a fresh single-use link token **and revokes whatever access the panel currently grants** — it nulls `federation_token` and `linked_at` unconditionally and reports `revoked` in its reply. There is no separate rotation route and no 409: before wave 4b the endpoint refused once `federation_token` and `linked_at` were both set, which made a linked node's token unrevocable except by editing `federation_config` over SSH. The token handed to the admin is `base64url("<panel_url>|<raw_token>")`, where `panel_url` comes from the node's own `PANEL_DOMAIN` + `PANEL_SECRET_PATH` (`_build_panel_url`, falling back to `request.host`) — so a wrong `PANEL_DOMAIN` on a node sends the master to the wrong address, and the failure only surfaces as a handshake timeout. The node's System → Link card shows that URL, which is the only place an admin can catch it.

On the master, `POST /api/panels/<id>/relink` (admin JWT) decodes that token, handshakes, and writes the new `federation_token` **and** URL into the **existing** `LinkedPanel` row. **Never re-link by deleting and re-adding the panel:** `delete_panel` runs `purge_tariff_items(TariffItem.panel_id == …)`, which removes every `TariffItem` of that panel and disables any tariff left with none — revoking a credential would cost live users their tariff layout. `tests/test_federation_token_rotation.py` asserts the `TariffItem` count survives; note that asserting the row's `id` is unchanged does **not** catch delete-and-add, because SQLite reuses the rowid when the deleted row was the only one (the guard leans on `created_at` and the tariff count instead). `relink` writes no status — the cron service owns `LinkedPanel.status`; it publishes on `panel:refresh` and lets the poll do it.

Between the revoke and the re-link the node is unreachable to the master **entirely**: not polled, not provisioned, not managed, and `poll_linked_panels` marks it `offline` with the 401 as `last_error`. Provisioning in that window raises, so a payment stays `pending` and `poll_pending_payments` re-applies it after the re-link.

`POST /api/federation/handshake` carries `@limiter.limit("30 per minute")` — it is the only unauthenticated route the node serves, and `Limiter` has no `default_limits`. Until wave 5d that also made handshake answer **500** when the node's own Redis was unreachable, which is precisely the state a node is in while first being brought up; the in-memory fallback (see Docker Services) removed that without removing the limit, and the node now answers with its own `401 no pending link token`.

**A snapshot is written twice: the live key with its TTL and a last-known copy with none.** `store_panel_snapshot` sets `panel:<id>:snapshot` (60s) alongside `panel:<id>:snapshot:last` (no TTL), and `panel:<id>:last_poll` (300s) alongside `panel:<id>:last_poll:last` (no TTL). `get_panel_snapshot` prefers the live key and falls back to the last known one, logging one WARNING per outage (cleared when a fresh key reappears, so a second outage warns again). Without this a cron-host outage longer than 60 seconds emptied every reader at once: the sub role builds a config from its own `Client` rows, of which a split deployment has none, and answered `404 "User not found"` — a client app reads that as *this subscription does not exist*, not as *try later*. **The accepted cost is that a client disabled or deleted while the pipeline is down keeps being served** until it returns; that is the same shape as the 60s subscription cache and the 24h `profile-update-interval`, and it is why the fallback is never silent. `get_panel_liveness` answers `("stale", <last successful poll>)` when the live status key is gone but a last-known copy exists, which is what puts the amber `Stale` badge and its explanation on the master's Panels card — before, the page fell back to `LinkedPanel.status` in Postgres and showed a node nothing had contacted for hours as `online`. `forget_panel` deletes all five keys: the two TTL-less ones would otherwise outlive the panel forever and a panel re-added under the same id would be served a stranger's inbounds. **None of this reaches `fetch_panel_snapshot_live`**, which exists so that a block or a revoke can never silently no-op — there, out-of-date data is a defect, not resilience.

**This does not survive the data-tier Redis itself dying, and cannot.** The last-known copy lives in that Redis, so if it is gone both keys are gone. The master mirrors no node clients (`Client` has no `panel_id`), so there is nowhere else to read them from; closing that would mean mirroring node state upward, which is a different change entirely.

**The provisioning contract carries two semantics, and which one you send decides who computes the expiry.** `POST /api/federation/provision` accepts `period_ms` **or** `expiry_ms` — exactly one; both or neither is a `ValueError` → 400. `period_ms` means *extend*: the node computes `max(now, client.expiry_time) + period_ms` itself, which is the only place the arithmetic can be correct, because an orchestrator's database holds no `Client` rows for node-issued clients (`Client` has no `panel_id` and the master does not mirror them). `expiry_ms` means *assign that exact date*, and exists for `backfill_tariff`, whose meaning is "give this user the same expiry he already has on his other nodes" — sending a period there would hand him `held_until + period` and drift one tariff's dates apart per node. Do not "simplify" the endpoint down to one field.

`period_ms` additionally **requires an `idempotency_key`**, and the rule generalises: *a key is required exactly where the operation is not idempotent on its own.* Assigning an absolute date is idempotent by construction; adding a period is not, and the retries are routine — `poll_pending_payments` re-runs a partially-failed multi-node grant every 30s, and the nodes that already succeeded are never rolled back (`provisioning.py`'s remote loop raises on the first failure). The node stores the key with its own reply in `provision_receipt` (unique on `(idempotency_key, inbound_tag)`) and replays that stored reply on a repeat, adding nothing — the reply matters because `apply_payment` puts its `expires_at_ms` into the `payment_succeeded` event the bot shows the user. There are two layers here: the fast path reads the receipt before mutating, and a concurrent request that slips past it fails the unique constraint, rolls back and returns the winner's result. Removing either one alone leaves the suite green, so do not delete the `IntegrityError` branch as dead code.

Callers pass a **natural key per entry point** (`operation_id` on `apply_tariff_for_user`, mandatory): `pay:<payment_id>`, `trial:<tg>:<tariff_id>`, `grant:<uuid>`, `backfill:<grant_id>`. The first is stable across exactly the automatic retries that exist; an admin grant has no automatic retry, so a repeat is intent rather than a fault. A grant carries **no key at all** — it sends `expiry_ms` (assign that exact date, `0` = never), which is idempotent by construction, and a stored receipt would replay the first date the second time an admin edits the term.

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

**Nothing is published that the consumer has no branch for.** `config_changed` (admin saves Bot → Settings) and `trial_activated` (user claims the trial) were published and both fell into the dispatcher's `else: return`, each leaving a `bot_event` row until the cleanup cron pruned it. Both are gone (wave 5b, §69): the bot re-reads its runtime config on its own 60-second poll, and it shows the trial's outcome from the HTTP response it is already waiting on. `backend/tests/test_events_without_a_consumer.py` holds both ends — the admin action leaves the table empty, the consumer has no branch — plus a positive control (blocking a user still writes exactly one `user_blocked` row), because "no rows" is also what a broken publisher looks like.

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

**The per-UUID route builds a node client's config from the snapshot, not from the node.** A `Client` row for a client issued on a node exists only in that node's SQLite, so the route first looks locally and then falls through to `_remote_pair_for_uuid`, which finds the client in the cached `panel:<id>:snapshot` and builds all three formats (raw base64, Clash, sing-box) out of it. The predecessor — `_try_proxy_sub_to_child`, an HTTP `GET /api/sub/<uuid>` against the owning node with `timeout=8` — is gone: a dead node used to stall a live user's request for eight seconds. The accepted cost is that `subscription-userinfo` counters are now up to `SUB_CACHE_TTL_SECONDS`+poll-interval stale rather than live from the node's own database; against the `profile-update-interval` of 24h announced to the client that is noise. **A missing snapshot still means a 404 on that route** — nothing else can answer it — but since wave 5d "missing" no longer follows from the cron host being down for a minute: the read falls back to the TTL-less last-known copy (see Panel Federation). It follows only from the data-tier Redis being unreachable, or from that panel never having been polled at all.

That same `/api/sub/u/<token>` URL serves two audiences off one route: a client app's User-Agent gets the raw config, a browser gets the **React subscription page** — `frontend/packages/sub-page`, built by `backend/Dockerfile.sub` and baked into `panel-sub` at `/app/ui` (override with `SUB_PAGE_DIST`), with its assets under `/api/sub/u/assets/…`. The page is a static bundle and holds no data of its own; it fetches `GET /api/sub/u/<token>/info` for the JSON it renders (traffic used/limit, expiry, per-node entries, deep-links). A missing bundle 503s the page and its assets **without** touching config delivery — the sub role's critical function stays alive. The server-rendered HTML page this replaced is gone; there is no `<!doctype html>` left in the Python.

**Only the `sub` role serves subscriptions at all.** `roles/sub.py` registers the `subscription` blueprint and no other role does; `panel-sub` is also the one image that bakes the page bundle, so the role that serves the routes is exactly the role that can render them. Until wave 3b `roles/master.py` and `roles/worker.py` registered the same blueprint with no bundle behind it, which cost three things at once: an unauthenticated endpoint on an admin host, a **three-image** rebuild for every edit to the subscription code, and a browser branch that answered 503 where a page was expected while the client-app branch kept returning configs — a failure quiet enough to survive a release. All three are gone, and `panel-sub` is no longer a dependency of `panel-master` or `panel-worker`. **`SUB_DOMAIN` is therefore load-bearing:** with it empty no subscription link is produced anywhere, for a browser or for a client app. Baking the bundle into `master`/`worker` instead was considered and rejected: three images would carry a Node build stage to serve a page two of them have no business serving.

### Custom Select component
`frontend/packages/ui-core/src/components/ui/Select.tsx` renders a portal-based dropdown instead of a native `<select>`. It synthesizes a `React.ChangeEvent<HTMLSelectElement>` in its `onChange`. When used with react-hook-form, always spread `{...register('fieldName')}` so the `name` prop is passed — react-hook-form looks up the field by `event.target.name` and silently ignores the change if `name` is missing or empty.

### Default outbounds
On startup **of a node**, `direct` (freedom) and `block` (blackhole) outbounds are auto-created if missing, and re-enabled if an admin disabled them — do not delete them there; every Xray config needs both.

**The master does the opposite, and has since wave 4c-2.** `roles/master.py` calls `bootstrap_defaults(app, system_outbounds=False)`, which not only skips the seed but *deletes* any `direct`/`block` row it finds — those two rows are sitting in the Postgres of every panel that ran an earlier release, and skipping the seed alone would leave `GET /outbounds` on the master answering `[direct, block]` forever. The flag lives at the **call site**, not inside `bootstrap_defaults`: both roles call the same function, so switching the seed off in the shared body would silently disarm every node. The removal is narrow on purpose — only those two tags, so a boot-time `DELETE` against a live database cannot reach anything else. A master has no Xray to route with; its outbounds and routing profiles live on the nodes and are edited through `?panel_id=` (see Panel Federation).

### Xray settings, config and per-user routing are answered by the node that runs Xray

`xray_log_level`, `geoip_url` and `geosite_url` are `SystemSetting` rows, but their only reader is `generate_config_file()`, which runs on a node against that node's own SQLite. Wave 5b gave `GET`/`PUT /api/system/settings` and `GET /api/config` the `has_local_xray()` gate every neighbour in `system.py` already had, because on the master each of them lied in its own way: the `PUT` wrote the keys into the shared Postgres, called a `generate_config_file()`/`restart_xray_container()` pair that `RemoteXrayGateway` answers with `None`, and returned **200 with the updated form**; the `GET` handed back the master's own copy of the same dead keys; and `/api/config` opened `/etc/xray/config.json`, which the `panel-master` image does not contain, and answered `404 "Config file not found"` — loud, but misleading exactly as `"DB not found"` was in the pre-4c-1 backup path.

**Wave 5c added the half above that gate, and the gate was not rewritten (§82).** Six handlers now dispatch on `?panel_id=` *before* consulting `has_local_xray()`, the shape 4c-2 and 4d established: `GET`/`PUT /api/system/settings`, `GET /api/config`, `POST /api/system/update-geo` and `POST /api/restart` in `system.py`, plus `POST /api/user/routing` in `auth.py`. The master with no node named still answers **501**, and the message names `panel_id`. The order is load-bearing: hoisting the gate above the dispatch turns the whole master side back into 501 and is invisible on a node, where both orders behave identically — which is why every dispatch assertion in `backend/tests/test_xray_control_over_federation.py` is built against the **master** role app, and why every route is asserted individually rather than by sample (§80).

**`GET /api/logs` is deliberately not in that list.** It is a stream — the node yields SSE lines and sleeps when the log is quiet — while both `FederationClient` methods end in `.json()`. Proxying it means a `requests.get(stream=True)` relay like `panels.py`'s `panel_backup`, plus a greenlet on the master living as long as the admin's tab, plus a decision about read timeouts against a stream that legitimately says nothing for minutes. Customer decision: out of wave 5c. The log panel in `System.tsx` therefore stays the one `hasLocalXray` block left on that page.

**The read half of `/user/routing` needed the federation snapshot to carry `preferred_outbound`.** The master shows a node client's current route from the cached snapshot, and `api/federation.py` did not put the field in while `services/remote_clients.py` hardcoded `""`. Without both, an admin sets a route, saves, reopens the modal and reads "Default (No preference)" — the write works and the screen lies. Both now carry it.

`backend/tests/test_xray_settings_are_node_only.py` (5b) pins the refusals; `test_xray_control_over_federation.py` (5c) pins the dispatch, the node-side credential, the journal and the snapshot field; `test_system_page_reaches_the_nodes.py` pins the bundle.

### Database migrations

**Exactly one service migrates each database, and which one is decided by ownership.** The shared Postgres is migrated by the **cron service and nothing else** (`roles/cron.py` → `app_base.migrate_schema`); a node's own SQLite is migrated by that node (`roles/worker.py`), because nothing central can reach a file on a node's disk. The master, sub and bot-api migrate nothing. The master still seeds *defaults* into an existing schema (`bootstrap_defaults` — admin row, `bot_service_token`, the `direct`/`block` outbounds) but calls `_require_schema()` first and **refuses to start on a virgin database**, naming the deploy order in the error. So the order is now load-bearing and loud: **data tier → cron → master, sub, bot-api**. Before wave 2 the master created the schema, which made sub and bot-api answer 500 until it had booted once, with nothing anywhere saying why.

For local development on SQLite this means `uv run python run.py` on an empty database fails the same way — run `uv run python migrate_db.py` first, or bring up the cron role once.

`panel_core.db_migration` (standalone entrypoint: `backend/migrate_db.py`) is a custom migration system (not Flask-Migrate). Current schema version is **`27`**, tracked via `PRAGMA user_version`. The script is idempotent — runs on every backend startup, uses `CREATE TABLE IF NOT EXISTS` for new tables and `ALTER TABLE ADD COLUMN` (with `_add_column_if_missing` guard) for column additions. All `ALTER`s are SQLite metadata-only (O(1)), so migration time is independent of row count. When adding a new table: add a `_ensure_<name>_table` function, call it from `migrate_sqlite_db`, bump `CURRENT_DB_VERSION`. **Retired tables are listed once, in `RETIRED_TABLES`**, and dropped by both migration paths from that one list (`_drop_retired_tables`); it currently holds `node_traffic_snapshot` and `client_device`, both retired by wave 3b. `create_all()` never removes a table, so a retirement that is not in that tuple simply lingers forever on every live database.

**The Postgres side is a different mechanism, and since wave 9 it finally reaches columns too.** `migrate_postgres_db` (`pg_migrate.py`) is `db.create_all()` + `_add_missing_columns()` + dropping FK constraints + recording `schema_version` + seeding bot texts. `_add_missing_columns` diffs `db.metadata` against `information_schema` and emits one `ALTER TABLE … ADD COLUMN` per column the models grew, so a **new column on an existing table** now arrives on a live Postgres the same way a **new table** always did. Two boundaries are deliberate and load-bearing: it **never drops** anything (a column present in the database but absent from the models simply stays), and a column declared `NOT NULL` **without a `server_default`** is added *nullable* with a WARNING rather than failing — an existing table cannot be back-filled from here, and a start-up refusal on the cron service would take the whole schema with it. So give every new column a `server_default` if you want it `NOT NULL` in Postgres; `materialized` on `provision_receipt` (wave 9) is the worked example.

The history matters because it shaped two live tables: while the limit stood, the wave-3a idempotency key was made a table rather than a column on `Client`, and wave 3b's device ledger became the new table `user_device` rather than a `telegram_id` column on `client_device`. Neither is worth undoing — both shapes are right on their own merits — but the *reason* recorded in their tests is now history, not a constraint. A **table drop** was never in the same bind: wave 3b added an explicit `DROP TABLE` to both paths.

**`PG_DEAD_TABLES` is the other half, and it is guarded by who asks for it.** `traffic_snapshot`, `domain_stat` and `notification_log` are written only by jobs `roles/worker.py` registers, against a node's own SQLite; on the shared Postgres they had no reader at all (statistics answers 501 before touching the database, `_require_local_xray` refuses before a `NotificationLog` is cleared). Wave 9 keeps them out of `create_all` and drops them — but only when the caller passes `drop_dead_tables=True`, and only `roles/cron.py` does. The flag lives at the call site for the same reason `bootstrap_defaults(system_outbounds=False)` does: `docker-compose.node.yml` merely omits `DATABASE_URL` rather than forbidding it, so a worker pointed at Postgres takes this very path, and these three tables are the only ones it genuinely writes. **A dead table that holds rows is never dropped** — an installation carried over from the monolith keeps real traffic history there, so a non-empty one survives and the cron service logs its row count instead.

Bot texts have their own version: `CURRENT_BOT_TEXTS_VERSION = 20`. A bump triggers a one-shot **force-reseed** (only when `stored < CURRENT`): it DELETEs the `_REMOVED_BOT_TEXT_KEYS` tuple (purging orphan rows for keys dropped from the YAML) and then upserts every `(key, lang)` pair from `app/data/bot_texts_defaults.yaml` (~74 keys × RU/EN). The upsert **preserves admin-edited rows** — `bot_text.customized` (set to `1` whenever an admin saves a text via Bot → Texts) is honoured by `ON CONFLICT … DO UPDATE … WHERE customized = 0`, so a force-reseed refreshes only untouched defaults and never reverts customizations. On the v19 migration that added the column, rows whose stored text already diverged from the YAML default are back-filled `customized=1` to protect pre-existing edits. When you remove a key from the YAML, append it to `_REMOVED_BOT_TEXT_KEYS` (the purge ignores `customized`, since a removed key is dead regardless). **Nothing else ever deletes a bot text**, so a key dropped from the YAML without that entry survives on every live database as an orphan row the admin can still edit in Bot → Texts; `tests/test_bot_texts_defaults.py::test_every_key_dropped_from_the_yaml_is_listed_as_retired` diffs the YAML against `HEAD` and fails on one. It compares against `HEAD`, not against the last release, so it catches the omission in the wave that made it — not later.

> **Reseed gotcha:** the purge/overwrite only fires when `stored < CURRENT`. An install already **at** the current number but with older content (e.g. a dev box that ran an unreleased build at the same version) is skipped — new keys still appear via the non-force `INSERT OR IGNORE` seed, but removed/changed keys don't. Coming from a real release baseline it's always clean; to force a clean reseed on such a dev box, set `system_setting.bot_texts_seeded_version` below CURRENT and restart the backend. To guarantee a reseed on *every* install regardless of prior unreleased numbers, bump strictly above the highest number any box has stored.

### Python dependencies & Docker images (uv)
Both Python services (`backend/`, `tg_bot/`) are **uv projects**: dependencies live in `[project].dependencies` in each `pyproject.toml`, pinned by a committed `uv.lock` (reproducible builds — previously every rebuild floated to latest). `[tool.uv] package = false` marks them as applications (install deps only, no wheel build), and `requires-python = "==3.12.*"` matches the `python:3.12-slim` base and the `grpcio==1.66.2` pin. There is **no `requirements.txt`** — `uv sync` is the install path everywhere.

Dockerfiles are **multi-stage**: a builder stage runs `uv sync --frozen --no-dev` into `/app/.venv`, then the final stage copies only `/app/.venv` + code — no `uv` binary, no `git`, no `build-essential` in the runtime image. The backend now builds as **four** per-role images from **three** Dockerfiles instead of one monolithic `backend` image: `backend/Dockerfile` takes a required `ARG PANEL_PACKAGE` (no default — an empty value fails the build via `test -n "$PANEL_PACKAGE"`) and is reused for `panel-master` (**211 MB**, 33.4% smaller than the monolith) and `panel-bot-api` (**222 MB**, 29.9% smaller) by passing `--build-arg PANEL_PACKAGE=panel-{master,botapi}`; `backend/Dockerfile.worker` is a **separate file**, not another `PANEL_PACKAGE` value, because only the worker needs the Xray binary (`COPY --from=xraybin`) and the `grpc_tools.protoc`-generated protobuf stubs (`XRAY_CORE_REF`-pinned), which would otherwise bloat the light images — it hardcodes `--package panel-worker` and produces `panel-worker` (**311 MB**, still under the monolith but only 1.8% smaller, since it alone keeps the Xray runtime); `backend/Dockerfile.sub` is the third, and likewise takes no `PANEL_PACKAGE` (it hardcodes `--package panel-sub`), because it alone carries a `node:20-alpine` stage that builds `@panel/sub-page` and bakes the bundle into `/app/ui` — `panel-sub` measures **210.3 MB**, up 437 KB from the **209.9 MB** it was when it still built off the shared Dockerfile, which is the bundle and nothing else (the Node stage never reaches the runtime image). The old monolithic `backend` image was **316.8 MB**; state the four measured sizes rather than a percentage range when reasoning about this, since the range drifts every time one image changes independently of the others. The `uv` binary comes from the pinned `ghcr.io/astral-sh/uv:0.11.19` image; `UV_LINK_MODE=copy` keeps the venv relocatable across stages, `/app/.venv/bin` is first on `PATH`, and a `.dockerignore` keeps the local `.venv`, `secret.key`, `tests/` and `db/` out of the build context. `UV_PYTHON_DOWNLOADS=0` forces uv to use the base image's interpreter. All three Dockerfiles insert a dependency-only cache layer between the `pyproject.toml` COPYs and `COPY packages/`, so an app-code change doesn't invalidate the dependency-install layer — but they sync a different package there: `backend/Dockerfile` syncs `--package "$PANEL_PACKAGE"` (whichever role is being built), while `backend/Dockerfile.worker` and `backend/Dockerfile.sub` hardcode `--package panel-worker` / `--package panel-sub` in that cache layer too, the same way their main sync steps do. There is also a **repo-root `.dockerignore`** now, which every `--build-context project=.` draws through: the other Dockerfiles take only `versions.json` from that context and never noticed it was unfiltered, but `Dockerfile.sub` copies all of `frontend/` out of it, and the host's `frontend/node_modules` would otherwise be shipped in (212 MB) and land on top of the `npm ci` the ui stage just ran. The bot image is unaffected by this split (~176 MB).

CI installs uv via `astral-sh/setup-uv@v8.2.0` and runs `uvx ruff` for lint, `uv sync --frozen` + `uv run pytest` for tests. `uv sync` installs the dev group (`pytest`, `pytest-flask`) — note `pytest-flask`'s autouse fixtures pull the `app` fixture ahead of other autouse fixtures, so test mocks must patch a name **where it is used** (`app.api.inbound.restart_xray_container`), not only where it is defined (`app.services.xray.*`); the source-module patch silently misses because `api/inbound.py` did `from app.services.xray import …`.

### Statistics storage
`TrafficSnapshot` stores hourly traffic deltas per entity (user or inbound) **forever** — space is ~100 bytes × entities × 8760 hours/year, negligible for typical deployments. `DomainStat` stores daily domain hit counts and is pruned to 90 days. Both use SQLite `ON CONFLICT DO UPDATE` upserts via `literal_column()` + raw `text()` SQL — do not replace with ORM insert, it breaks atomicity.

**Both tables live on a node and only on a node, and everything about the statistics surface follows from that.** Their only writers are `_upsert_snapshot` and `_upsert_domain_stat`, called from `services/stats.py`, which ships from `panel-worker` and runs under `sync_traffic` (10s) and `parse_logs` (15s) — jobs `roles/worker.py` registers and no other role does. Nothing has ever written either table into the shared Postgres. So the master's copies are empty by construction, not by accident, and no amount of waiting fills them.

Since wave 4d the five `/api/stats/*` handlers therefore behave like `outbound.py`'s: `?panel_id=` is dispatched to the named node over federation **before** the `has_local_xray()` gate is consulted, and a master with no node named answers **501** naming `panel_id` rather than 200 with zeroes. That distinction is the whole point — a zero and a real answer differ only in the response body, so a test asserting `200` would pass against the defect. `panel_proxy` gained five read-only `proxy_stats_*` functions (26 → 31); like wave 4c-2's three reads they deliberately do **not** publish on `panel:refresh`, because they change nothing on the node and run on every page load.

**`_top_domains_sql` is dialect-aware, and the hint it emits is not decorative.** `domain_stat` carries two indexes, and without `INDEXED BY ix_ds_date_domain_cover` SQLite's planner picks the narrower `ix_ds_domain` and stops covering the query. But `INDEXED BY` is SQLite-only syntax: against Postgres it is a syntax error, which is why `/stats/overview` and `/stats/domains` — the two of the five that call this function — answered **500** there while the other three answered 200 with nothing (verified by running both directions against `postgres:16`). The hint is now emitted only when the bound dialect is `sqlite`, and an undeterminable dialect falls back to portable SQL — failing towards a slower plan rather than towards a syntax error. Do not "simplify" it back to one form; both halves are load-bearing and each is guarded in `tests/test_api_statistics.py`.

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
| caddygen | `cd caddy/caddygen && go vet ./... && go test -count=1 ./...` |
| Shell lint | `shellcheck --severity=warning $(git ls-files '*.sh')` |
| Dockerfile lint | hadolint (runs in CI only) |

CI provisions uv via `astral-sh/setup-uv@v8.2.0` (there is no moving `v8` major tag — pin the exact version), then runs the commands above through `uvx` / `uv run`.

`uvx ruff format <dir>` and `npm run format` auto-fix formatting issues — run them before committing, not after CI fails. markdownlint is **not** run in CI.

**Two jobs cover what nothing covered until wave 12, and both guard code that decides whether a deployment works at all.** `caddygen` was the only component with tests and no CI job — while it generates the Caddy config that decides which domains get a certificate and where the payment webhook lands. `-count=1` is not tidiness there: `compose_test.go` reads `docker-compose.*.yml` and `caddy/routes.yaml` from **outside** the Go module, the build cache does not track them, and a plain `go test` will print `ok (cached)` for a config that no longer generates. The shell job takes its file list from `git ls-files '*.sh'` rather than a fixed set, so a script cannot be added without being linted — it exists because `scripts/install.sh` is what a deployer pipes into bash as root, and it had no static analysis of any kind.

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

   **⚠️ NO LONGER TRUE — wave 11 gave Caddy ACME, so nothing below about issuing certificates by hand applies.** The bot host still needs a certificate for `BOT_DOMAIN` and still needs `:80` reachable, but Caddy obtains and renews it. Kept for the reasoning about why a shared script could not serve four hosts.

   **The bot host needs its own certificate covering `BOT_DOMAIN`, issued manually on the box** — it is not distributed from the master. There was a `scripts/generate_certs.sh` when this note was written and it could not do it either: it passed `-d "$PANEL_DOMAIN"` unconditionally, whose DNS points at the master, so the `certbot --standalone` HTTP-01 challenge for it was answered by the master's Caddy and certbot — being all-or-nothing — failed the whole run; and its `docker compose stop caddy` / `up -d caddy` carried no `-f`, so on a bot box they resolved the legacy monolithic `docker-compose.yml` and replaced `panel-bot-caddy`. Wave 10 deleted the script and the file it resolved; every host does this by hand now, and the bot box is no longer the exception:

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

6. **The legacy monolithic stacks are dead — and since wave 10 they are also gone.** `docker-compose.yml`, `docker-compose.prod.yml`, `docker-compose.staging.yml`, `scripts/install_{dev,prod}.sh` and both cert helpers were deleted outright. They set `PANEL_ROLE=master` alongside a local `xray` container and pointed the bot at `backend:5000`, where `/bot-service/*` no longer exists; they were **already** broken before this phase, since phase 3b gives the master a `RemoteXrayGateway` and registers none of `sync_traffic` / `check_limits` / `parse_logs`, so a local `xray` was neither driven nor polled. They were kept frozen for three phases on the reasoning that a from-scratch installer would replace them, which turned the repo into a place where the most obvious command (`docker compose up`) started a stack that could not work. Deleting them makes that command fail with *no configuration file provided* instead. **The replacement — install + management scripts — is the next piece of work, and this file is not it.**

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

1. **`.env.example` is gone; there are five files now.** `.env.master.example`, `.env.node.example`, `.env.sub.example`, `.env.bot.example`, `.env.data.example`. Copy the one matching the box, onto that box only. A shared `.env` was never correct: `RATELIMIT_STORAGE_URI` must be the box's *own* Redis on the master and on a node and the *data tier* on the sub and bot hosts, and the old file carried both values at once — one live, one commented out — leaving the contradiction for the deployer to resolve. The per-host files carry no commented alternatives, so a variable that does not belong on a host is simply absent. The legacy monolithic `docker-compose.{yml,prod,staging}.yml` and `scripts/install_{dev,prod}.sh` still referenced `.env.example` when this note was written, with no example file left to download; wave 10 deleted all five (see point 6 of the 3c-2 note).

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
   (60s), `reset_grant_traffic_cycles` (15m), `cleanup_bot_events` (24h), `check_latest_version` (6h).
   **While it is down nothing polls the nodes**, so every `panel:<id>:snapshot` expires after 60s and
   the subscription shrinks to whatever the serving role holds locally; free tariffs stop renewing and
   undelivered bot events stop being replayed. That is the same exposure the master used to carry — it
   moved, it did not disappear.

2. **Put it next to the data tier, not on it.** The cron service is the first thing in this deployment
   that needs outbound HTTP to every node (for the poll, and for provisioning inside
   `reset_grant_traffic_cycles`). The data VM's whole value is that it has no outbound connections at all;
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

5. **A node whose own Redis is down can no longer be linked. ⚠️ NO LONGER TRUE — fixed by wave 5d.**
   `POST /api/federation/handshake` gained a `30 per minute` limit, and Flask-Limiter was configured not to
   swallow storage errors, so an unreachable `RATELIMIT_STORAGE_URI` turned the handshake into an `HTTP 500`
   (verified by running it against a closed port). Handshake was previously the one route that touched nothing
   but its own database, and this bit exactly the half-started node you are looking at when first bringing one
   up. Wave 5d's in-memory fallback removed it without removing the limit: the same node now answers its own
   `401 no pending link token` (verified the same way). Kept here because a fleet mid-upgrade still contains
   nodes on the older image.

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

### Deploy note — a node's network is configured from the master, and the federation token can now redirect traffic (Phase 8 wave 4c-2)

This wave changes **no schema, no federation contract and no environment variable**, so it needs no
fleet-wide lock-step and no `.env` edit. It hands the master a capability it never had, deletes two
rows from every live master's database, and — the part to read before anything else — **widens the
federation token a second time, in the direction that leaves no trace**. Read all seven points.

1. **The federation token can now rewrite where a node's traffic goes.** Twelve more handlers on the
   node accept it: outbound create/update/delete, balancer create/update/delete, routing-profile
   create/update/delete, plus `GET /outbounds`, `GET /balancers` and `GET /routing-profiles`. Wave
   4c-1's widening was "read and replace the node's whole database", which is loud — the file changes,
   the admin password stops working. **This one is quiet.** A holder can point a routing rule at an
   outbound of their own and send a user's traffic anywhere, and nothing in the client list, the key
   list or the subscription changes: the user's config still names the same node, the same UUID, the
   same port. The only place it shows is the node's own Routing page and the log line in point 5.

   The token sits in the master's Postgres **in clear text** — it has to, or the master could not
   present it — so it travels in every `pg_dump` of the data tier, and it is not scoped per operation.

   **How to kill one — the full wave-4b procedure, not a reference to it:**
   1. On the **node**: System → Link → *Revoke access & issue token*. That nulls `federation_token`
      and `linked_at` on the spot (there is no confirmation step and no undo) and hands you a fresh
      single-use link token.
   2. On the **master**: Panels → that panel's card → *Relink*, and paste the token.
   3. **Never delete the panel and add it again instead.** `delete_panel` cascades
      `purge_tariff_items`, which removes every `TariffItem` of that panel and disables any tariff
      left with none — revoking a credential would cost live users their tariff layout.

   Between step 1 and step 2 the node is unreachable to the master **entirely**: not polled, not
   provisioned, no user or inbound CRUD, and now no routing management either. `poll_linked_panels`
   marks it `offline` with the 401 as `last_error`. A purchase landing in that window raises, the
   payment stays `pending`, and `poll_pending_payments` (30s, on the bot host) re-applies it after the
   relink — money is not lost, but the user waits. Keep it to one sitting.

2. **Two rows are deleted from every master's database on first boot.** The seeded `direct` and
   `block` outbounds are removed by `bootstrap_defaults(app, system_outbounds=False)` — see Default
   outbounds above. Nothing reads them on a master (it has no Xray), and the removal is scoped to
   those two tags, so anything else an old monolith-turned-master holds is left alone. **Nodes are
   unaffected**: they still seed both and still re-enable them if an admin switched them off. Note the
   consequence in the panel: `GET /outbounds` on a master now answers `[]`, and the Routing page's
   Outbounds tab is empty until a node is selected.

3. **The Routing page is back on the master, and it edits a node, not the master.** Its header carries
   a node picker; every read and every write goes out with `?panel_id=`, live over HTTP to that node.
   Two things follow from "live", both deliberate:
   - **A node that does not answer produces an error box, not an empty list.** An empty list reads as
     "this node has no outbounds" and invites an admin to create them a second time on top of the ones
     already there. The page shows the node's own message and a Retry button.
   - **The node's validation messages arrive verbatim** — "Tag exists", "Rule #1 has unknown outbound
     target: …" — rather than a generic proxy error. Rules are validated **on the node**, against that
     node's outbounds, which is why the rule editor now offers only the selected node's inbound tags.

4. **`GET /outbounds/health` is node-only, on purpose (customer decision, carried over from 4c-1).**
   A reachability probe run from the master measures the *master's* route to an endpoint, not the
   node's, so it would be confidently wrong. The master answers 501 and says where to look; the
   health column simply does not render when a node is selected. To see it, open that node's own panel.

5. **The node writes down who changed its routing.** Every federated outbound/balancer/profile change
   leaves a WARNING on the node naming the credential and the source address; the node's own admin
   leaves an INFO. No new table and no schema change (§40 stays untouched) — the container's existing
   json-file rotation (50 MB × 5) covers it. This is the only durable record that a redirect happened.

6. **A partial rollout degrades in one direction and fails loudly in the other.** An old master against
   a new node: the master's Routing page is still hidden and nothing is proxied — no change. A new
   master against an old node: every call answers 401 (the old node still guards these with
   `token_required`, admin JWT only), and the master surfaces *"The node rejected this master's
   federation token. Issue a fresh link token on the node and relink the panel"* — misleading in this
   one case, because the node is simply old. Update the node. Nothing is silently corrupted either way.

7. **Bump `master`, `worker`, `sub`, `bot_api`, `cron`, `frontend_admin` and `frontend_node`
   together.** The `panel-core` edits (`utils.py`, `app_base.py`, `services/panel_proxy.py`) fan out to
   all five backends; `panel-adminapi` (`outbound.py`, `routing.py`, `inbound.py`) to master and
   worker; `panel-master` (`roles/master.py`) to its own image; the `ui-core` edits (`Routing.tsx`,
   `Sidebar.tsx`) plus both `App.tsx` files to both frontends. `bot` and `caddy` are untouched.

### Deploy note — an undeliverable tariff stops before the money, and three env comments stop lying (Phase 8 wave 5a)

This wave changes **no schema, no federation contract and no authorisation**, and it rebuilds two
images: `bot_api` and `bot`. It is safe to deploy on its own, in either order relative to anything
else. Read all five points.

1. **The visible change on an installation that still has monolith-era tariffs: they disappear from
   the bot's catalogue.** A tariff is now "undeliverable" if it carries no items at all, or if **any**
   item has no `panel_id` — bot-api runs no Xray, so such an item names no node. Those tariffs stop
   being listed, `POST /api/billing/checkout` answers `400 tariff_not_available` for them, and no
   `Payment` row is written. **This is a removal of something users could see and press.** Before the
   wave they could press it, pay, and get `payment_failed` with nothing issued — the money was taken
   first and the refusal came after. If you would rather sell them, the fix is the same one the
   master has been asking for since phase 3b: open Bot → Tariffs and set a `panel_id` on every item.
   The master already lists the offenders in a WARNING at start-up
   (`audit_tariff_items_without_panel_id`), and bot-api now logs the same detail whenever one is
   filtered out or refused.

2. **Users who already hold such a tariff keep what they have.** Nothing is revoked, disabled or
   expired by this wave — existing `Client` rows and their expiry dates are untouched. What those
   users lose is the ability to *renew* it from the catalogue, which never actually worked: the
   renewal purchase failed after payment. Free grants of such a tariff (`auto_renew_free_users` on
   the cron host) are **not** covered by this wave — that path still logs an ERROR and moves on, as
   it did before. Same for an admin grant on the master: it still answers HTTP 500. Both were left
   alone deliberately (customer decision): they take no money, and widening the check to them would
   rebuild all five backend images instead of one.

3. **`PANEL_SECRET_PATH` is no longer required on the sub and bot hosts.** It was a mandatory
   `${VAR:?}` in both compose files while nothing in either image could read it — the only module
   that does, `api/federation.py`, ships from `panel-adminapi`, which neither `panel-sub` nor
   `panel-botapi` depends on. **Nothing to do:** an existing `.env` that still sets it keeps working
   (both services also take `env_file: - .env`, so the value still enters the container; it is simply
   never looked up). Removing the line is optional tidying. The master and every node still require
   it, and there it is genuinely load-bearing.

4. **Four comments in the example files were wrong and have been rewritten; re-read them if you
   configured a host from them.** `.env.bot.example` and `docker-compose.bot.yml` claimed an empty
   `SUB_DOMAIN` falls back to `PANEL_DOMAIN` + the secret path and only breaks the browser page —
   that fallback was deleted in wave 3b, and an unset `SUB_DOMAIN` now means **no subscription link
   at all**, for a client app as much as for a browser. `.env.bot.example` also credited
   `PANEL_DOMAIN` to `federation._build_panel_url`, which is not in that image. `.env.{master,node}.example`
   described their local Redis as holding "the sub-cache this host reads"; neither host reads one,
   since only the sub role serves subscriptions. `.env.cron.example` gave the right conclusion
   (`sslmode=verify-full` is unconditional there) with the wrong mechanism.

5. **Bump `bot_api` and `bot` only.** The edits are confined to `panel-botapi`
   (`services/tariff_delivery.py`, `services/billing.py`, `api/bot_service.py`) and `tg_bot`
   (`handlers/catalog.py`). `master`, `worker`, `sub`, `cron`, both frontends and `caddy` are
   untouched. Deploy `bot-api` and `bot` together — they live in the same compose file. A new bot
   against an old bot-api still works (the catalogue is simply unfiltered); an old bot against a new
   bot-api also works, it just keeps the spinning button.

### Deploy note — the Statistics page starts answering from the machine that counted the traffic (Phase 8 wave 4d)

This wave changes **no schema, no federation contract and no environment variable**, so it needs no
fleet-wide lock-step and no `.env` edit beyond the image pins. It gives a page back to the role that
has the data, removes two fields that constrained nothing, and — the part to read before anything
else — **widens the federation token a third time, in the direction that is most sensitive and least
visible**. Read all seven points.

1. **The federation token can now read every user's traffic figures and browsing history off a node.**
   Five more handlers accept it: `GET /api/stats/{overview,traffic,domains,domain-users,users-ranking}`.
   Concretely that is per-client upload/download over any period, the e-mail of every client, and the
   **list of domains each of them visited** with hit counts — `domain_stat` is built from the node's
   Xray access log.

   The marginal increment is smaller than it sounds and worth stating exactly: wave 4c-1 already gave
   this token `GET /api/backup`, which streams the node's whole SQLite file, and `traffic_snapshot` and
   `domain_stat` are inside it. So the token could already read all of this — awkwardly, in one lump,
   and with a WARNING written to the node's log. What is new is that it can read it **selectively,
   cheaply, and silently**: these are reads, and following wave 4c-2's precedent for reads they are
   **not** journalled on the node. Logging them was considered and rejected on volume — the page issues
   seven requests per load and auto-refreshes every 30 seconds, so an audit line per request would
   drown the node's log and the entries that matter with it. If you want a durable record that someone
   pulled a node's browsing history, `/api/backup` is the path that leaves one.

   The token sits in the master's Postgres **in clear text** — it has to, or the master could not
   present it — so it travels in every `pg_dump` of the data tier, and it is not scoped per operation.

   **How to kill one — the full wave-4b procedure, not a reference to it:**
   1. On the **node**: System → Link → *Revoke access & issue token*. That nulls `federation_token`
      and `linked_at` on the spot (no confirmation step, no undo) and hands you a fresh single-use
      link token.
   2. On the **master**: Panels → that panel's card → *Relink*, and paste the token.
   3. **Never delete the panel and add it again instead.** `delete_panel` cascades
      `purge_tariff_items`, which removes every `TariffItem` of that panel and disables any tariff
      left with none — revoking a credential would cost live users their tariff layout.

   Between step 1 and step 2 the node is unreachable to the master entirely — not polled, not
   provisioned, no CRUD, and now no statistics either. Keep it to one sitting.

2. **What an admin sees on the master's Statistics page after the update.** The page is where it was,
   in the same menu slot, and it now carries a **node picker in its header**, exactly like Routing
   since 4c-2. The first enabled node is selected automatically, so on a single-node deployment the
   page simply starts showing numbers where it showed zeroes. Three other states are possible and all
   three say what happened rather than charting nothing:
   - **no linked nodes at all** → "No nodes to report on", pointing at the Panels page;
   - **the selected node does not answer** → a red box with *the node's own message* and a Retry
     button, on whichever tab was open. Never an empty chart: zeroes read as "nobody used this node",
     which is the exact lie this wave removes;
   - **a request that somehow reaches the master unscoped** → HTTP 501 naming `panel_id`.

   **There is no fleet-wide aggregate and this wave does not add one.** It is a switcher: one node at
   a time, its own numbers, unmixed. Summing two nodes' hourly buckets into one chart is a different
   feature.

3. **The page appears on every node's own panel, where it never existed.** A node's SPA went from four
   routes to five. That half is the actual bug fix: the node is the only machine in the deployment
   whose `traffic_snapshot` and `domain_stat` are full, and until now it had no screen to show them.
   An admin who prefers logging into the node directly (its own `Admin` row, its own password — see
   the node role) gets the same page with no picker.

4. **`Client.device_limit` and `Inbound.device_limit` disappear from the API and from both forms. The
   columns stay.** Nothing has enforced a per-key or per-inbound device cap since wave 3b — the gate
   counts one global budget per Telegram account, from `device_limit_enabled` and
   `device_limit_per_user` under Bot → Settings — but the panel kept accepting the values, storing
   them, and rendering them. **The visible change: the Dashboard's device chip loses its denominator.**
   Where it read `2 / 3` it now reads `2`; the count was always real, the cap never was. Two form
   fields are gone ("Device limit override" on a user, "Device limit (HWID-based)" on an inbound), and
   the API neither accepts nor returns the value.

   **Nothing is lost that was working**, but if you set those numbers expecting them to hold, they
   never did — check `device_limit_per_user` now, because that is the one that applies, and it applies
   to every account at once. Existing column values are left untouched and unreadable; dropping a
   column from an existing table is the one thing the Postgres migration path cannot do, so that is a
   separate change.

5. **Two SQL statements stop being SQLite-only.** `/api/stats/overview` and `/api/stats/domains`
   answered **HTTP 500** against Postgres, because they emitted `INDEXED BY`, which Postgres does not
   parse. In the shipped topology this is now unreachable from two directions at once — the master
   refuses before touching its database, and a node has no `DATABASE_URL` — so **do not read this as
   fixing a live outage**; it was live only for a Postgres-backed node, which `docker-compose.node.yml`
   does not produce. It is fixed because the statement travels in shared code and the hint is emitted
   only where it parses.

6. **A partial rollout degrades in one direction and fails loudly in the other.** An old master against
   a new node: the master's Statistics page has no picker and reads its own empty tables — unchanged
   from today, zeroes and two 500s. A new master against an old node: every read answers 401 (the old
   node still guards these with `token_required`, admin JWT only), and the master surfaces *"The node
   rejected this master's federation token. Issue a fresh link token on the node and relink the
   panel"* — misleading in this one case, because the node is simply old. Update the node. Nothing is
   silently corrupted either way.

7. **Bump `master`, `worker`, `sub`, `bot_api`, `cron`, `frontend_admin` and `frontend_node`
   together — seven images.** The `panel-core` edits (`services/panel_proxy.py`, `models.py`,
   `services/remote_clients.py`) fan out to all five backends; `panel-adminapi` (`api/statistics.py`,
   `api/inbound.py`, `api/federation.py`) to master and worker; the `ui-core` edits (`Statistics.tsx`
   moved in from `packages/admin`, `Sidebar.tsx`, `Dashboard.tsx`, `UserForm.tsx`, `InboundForm.tsx`,
   `lib/types.ts`) plus both `App.tsx` files to both frontends. `bot`, `caddy` and `xray_egress` are
   untouched.

### Deploy note — one answer to "when does my access end", and three handlers that stop claiming success (Phase 8 wave 5b)

This wave changes **no schema, no federation contract, no authorisation and no environment
variable**, so it needs no fleet-wide lock-step and no `.env` edit beyond the image pins. It carries
the wave's only user-visible change — a date some users will see move — plus four admin-side
refusals where the panel used to answer success. Read all seven points.

1. **The date a user is shown can move *earlier*, and that is the fix.** A user with keys on several
   nodes was given two different answers to one question: the bot took the **latest** expiry among
   them, the subscription page and the `subscription-userinfo` header a client app reads took the
   **earliest**. Someone with a 3-day key on one node and a 30-day key on another read "30 days" in
   Telegram and "3 days" in Hiddify. All three now answer with the **nearest** date (customer
   decision), because that is the one every key is still valid until, and the client app — the only
   surface with no per-node breakdown — is where an overstated date leaves a user staring at a dead
   server while the app says three weeks remain.

   **Concretely, what changes on an existing installation:** the bot's "payment received / access
   granted / gift / renewed" messages, and the trial's confirmation, may now name a nearer date than
   before for accounts whose keys expire on different days. Nobody loses access and no expiry
   changes — only the summary does. Accounts whose keys all expire together (the normal case for a
   single multi-node tariff) see no difference at all. The subscription page still lists every node
   with its own date, and the bot's Statistics screen still shows each key's own; only the one-number
   summary moved.

2. **An account with an unlimited key now reads "unlimited" everywhere.** `expiry_time = 0` means
   "never expires" and absorbs every dated key in the fold (§41). The subscription page and the
   header used to filter the zeroes out first, so an account holding one unlimited key and one dated
   key was shown the dated one while the bot said permanent. A related bot-side fix ships with it: a
   purchase on top of unlimited access used to produce **"access until ?"** — a literal question mark
   — and now says "♾️ Бессрочно" / "♾️ Permanent", reusing an existing text key, so
   `CURRENT_BOT_TEXTS_VERSION` does **not** move and no reseed happens.

3. **Three handlers on the master stop answering success: `GET`/`PUT /api/system/settings` and
   `GET /api/config` now answer 501.** Saving an Xray log level or a GeoIP URL on the master wrote
   the value into the shared Postgres, restarted nothing (the master's Xray gateway is remote and the
   call returns `None`) and answered 200 with the updated form; the only reader of those keys is a
   node, out of its own SQLite. `/api/config` answered `404 "Config file not found"`, which reads as
   *your config is gone* when the config is alive on the node.

   **Be clear about what this is: the ability to set a node's log level from the master did not
   appear — it became loudly impossible.** No button is lost, because the master's SPA never offered
   one (the whole Core tab and the View Configuration button are gated by `hasLocalXray`). What
   breaks is anything home-grown that called these three on the master; it was already doing nothing.
   To change a node's Xray settings today, open that node's own panel. Wave 5c is where `?panel_id=`
   dispatch makes it possible from the master.

4. **Two publications onto `bot:events` are gone: `config_changed` and `trial_activated`.** The bot
   had no branch for either; each one only filled a row in `bot_event` until the cleanup cron pruned
   it. Nothing changes for a user — the bot re-reads its runtime config on its own 60-second poll,
   and it shows the trial's outcome from the HTTP response it is already waiting on. If you have
   anything of your own subscribed to that channel and keyed on those two types, it stops receiving
   them.

5. **The master refuses two things it used to accept.** Saving a tariff with **no items at all** now
   answers 400 (the admin drawer already refused it client-side; the API was catching up). And an
   admin grant of a tariff whose item names no node — a monolith-era row with `panel_id IS NULL` —
   answers **400 naming the tariff and the inbound** instead of "Internal server error", and leaves
   no half-written grant behind. The remedy is the one the master has been logging at start-up since
   phase 3b: open Bot → Tariffs and pick a node for every item.

6. **System → About grows a bot row that was never there.** The bot's version travels through the
   shared Redis now (`panel:bot:status`, 180-second TTL) instead of a variable in bot-api's process
   that the master could never see. **Nothing to configure** — `SHARED_REDIS_URI` is already
   mandatory on both hosts. The row appears within a minute of the bot's next poll and disappears
   again if the bot goes quiet for three minutes; a bot that has never reported still shows no row,
   which is unchanged behaviour and deliberately not a red indicator (that belongs with the rest of
   System → About's health lines, wave 6).

7. **Bump `master`, `worker`, `sub`, `bot_api`, `cron` and `bot` — six images. Both frontends and
   `caddy` are untouched.** The `panel-core` edits (`services/expiry.py`, `services/bot_status.py`,
   `services/provisioning.py`) fan out to all five backends; `panel-adminapi` (`api/system.py`) to
   master and worker; `panel-master` (`api/bot_admin.py`) and `panel-botapi` (`api/bot_service.py`)
   and `panel-sub` (`api/subscription.py`) to their own images; `tg_bot` to `bot`. Deploy order does
   not matter and a partial rollout degrades quietly: an old bot against a new bot-api simply keeps
   printing "?" for an unlimited grant, and an old master against a new sub just goes on disagreeing
   about the date, which is today's behaviour.

### Deploy note — a node's Xray is configured from the master, and the federation token gets its fourth widening (Phase 8 wave 5c)

This wave changes **no schema, no environment variable and no federation contract shape** — it adds
one field to the snapshot, which is additive. It needs no fleet-wide lock-step beyond the image
pins. It hands the master a capability it never had, and — the part to read before anything else —
**widens the federation token a fourth time, and this time the most sensitive thing it gains is a
read that leaves no trace anywhere else.** Read all eight points.

1. **What the federation token can now do on a node.** Six more handlers accept it:
   `GET`/`PUT /api/system/settings`, `GET /api/config`, `POST /api/system/update-geo`,
   `POST /api/restart` (whose decorator was already this, since wave 0 — only the master-side
   dispatch is new) and `POST /api/user/routing`. In descending order of how much it matters:

   - **`GET /api/config` hands over the node's secrets in one request.** The generated
     `/etc/xray/config.json` carries `realitySettings.privateKey`, every WireGuard key and every
     client UUID. **The honest size of the increment is small** — `GET /api/backup` (wave 4c-1)
     already streamed the whole SQLite file, `stream_settings` and all. What is new is *selective,
     cheap and one button press*. Because of that, and against the wave-4d rule that reads are not
     journalled, **this read is journalled** (customer decision): the node writes
     `WARNING Xray config read (REALITY private key, WireGuard keys, client UUIDs) over the
     federation token from <address>`. The 4d rule stands for everything else; its reason was
     volume, and volume does not apply to a button pressed once a month.
   - **`PUT /api/system/settings` changes two things with non-obvious reach.** Setting the log level
     to `none` stops the node's access log, which is what `parse_logs` reads to fill `domain_stat` —
     so it silently switches off that node's visited-domain statistics without touching the
     Statistics page. And the GeoIP/GeoSite URLs decide what `geoip:ru`-style routing rules *expand
     to* — that is, they rewrite the meaning of the routing rules without editing a single rule.
     Paired with `POST /api/system/update-geo` (fetch from those URLs right now) it is a complete
     mechanism.
   - **`POST /api/user/routing`** pins one user's traffic to one outbound. After wave 4c-2 (create
     any outbound you like) this completes the picture: pick the destination, then pick the victim.
   - **`POST /api/restart`** adds nothing — the token already opened it.

   Every write above leaves a WARNING on the node naming the credential and the source address; the
   node's own admin leaves an INFO. That log line is the only durable record.

   The token sits in the master's Postgres **in clear text** — it has to, or the master could not
   present it — so it travels in every `pg_dump` of the data tier, and it is not scoped per
   operation.

   **How to kill one — the full wave-4b procedure, not a reference to it:**
   1. On the **node**: System → Link → *Revoke access & issue token*. That nulls `federation_token`
      and `linked_at` on the spot (no confirmation step, no undo) and hands you a fresh single-use
      link token.
   2. On the **master**: Panels → that panel's card → *Relink*, and paste the token.
   3. **Never delete the panel and add it again instead.** `delete_panel` cascades
      `purge_tariff_items`, which removes every `TariffItem` of that panel and disables any tariff
      left with none — revoking a credential would cost live users their tariff layout.

   Between step 1 and step 2 the node is unreachable to the master entirely: not polled, not
   provisioned, no CRUD, no statistics and now no Xray settings either. A purchase landing in that
   window stays `pending` and `poll_pending_payments` (30s, on the bot host) re-applies it after the
   relink. Keep it to one sitting.

2. **What an admin sees on the master after the update.** The System page grows a **Core** tab that
   was never there, and a **node picker** appears above the card whenever the Core or Maintenance
   tab is open. Under it: the node's Xray log level and its GeoIP/GeoSite URLs, and on Maintenance
   the *Update GeoIP*, *View Configuration* and *Restart Core* buttons, all acting on the selected
   node. The confirmation dialogs name it — "Restart the Xray Core service on **Amsterdam**?" —
   because one button with several possible targets has to say which one.

   Three other states are possible and each says what happened rather than showing an empty form:
   **no nodes linked** → a line pointing at the Panels page; **the node does not answer** → a red
   box carrying *the node's own message* and a Retry button; **a request that somehow reaches the
   master unscoped** → HTTP 501 naming `panel_id`.

   Security, About and the Maintenance backup card are **not** scoped by the picker: they are about
   the panel you are logged into, and always were.

   On the Dashboard, the **Route** button on a node client's row starts appearing. It was in the
   code all along and gated by `hasLocalXray`, so on a master it never rendered; the request it
   sends already carried `?panel_id=`. Its dropdown now lists the *selected node's* outbounds and
   balancers rather than the master's (which since wave 4c-2 are none at all), and the current route
   is read back correctly, because the federation snapshot now carries `preferred_outbound`.

3. **`GET /api/logs` is deliberately not in this wave — the live log panel stays node-only.** To
   watch a node's Xray log you still open that node's own panel. It is a stream, not a response:
   `FederationClient` ends in `.json()`, so relaying it means a streaming proxy plus a greenlet on
   the master that lives as long as the admin's browser tab, plus a policy for a stream that
   legitimately says nothing for minutes. Customer decision: separate work. **§66 is therefore
   closed for five of six routes plus `/api/restart`, not for all six.**

4. **Two behaviours to know before you press the buttons.** Saving a log level change on the master
   makes the node regenerate its config and **restart Xray** — every connection on that node drops,
   exactly as it does from the node's own panel. And *Update GeoIP* downloads two files (30s timeout
   each) and then restarts Xray as well; the master waits up to 110 seconds for it, comfortably
   inside its own 120-second gunicorn timeout, so a slow node produces a slow button and not a
   truncated request.

5. **One extra field on the wire: `preferred_outbound` in `/api/federation/snapshot`.** Additive, so
   a master left on an older image simply ignores it, and a node left on an older image omits it and
   the master shows "Default (No preference)" — the behaviour of every release up to now. Nothing
   breaks in either direction.

6. **§71 still blocks a private-address topology, and it is still not this wave's business.**
   `_validate_panel_url` refuses to add a panel whose URL resolves to a private address, so a master
   and a node on one private segment cannot be linked through the UI. Unchanged, recorded, and
   scheduled for the operational wave.

7. **A partial rollout degrades in one direction and fails loudly in the other.** An old master
   against a new node: the master has no Core tab and no Route button, so nothing changes. A new
   master against an old node: every one of the six answers **401** (the old node still guards five
   of them with `token_required`, admin JWT only), and the master surfaces *"The node rejected this
   master's federation token. Issue a fresh link token on the node and relink the panel"* — which is
   misleading in this one case, because the node is simply old. Update the node. Nothing is silently
   corrupted either way.

8. **Bump `master`, `worker`, `sub`, `bot_api`, `cron`, `frontend_admin` and `frontend_node`
   together — seven images. `bot`, `caddy` and `xray_egress` are untouched.** The `panel-core` edits
   (`services/panel_proxy.py`, `services/remote_clients.py`) fan out to all five backends;
   `panel-adminapi` (`api/system.py`, `api/auth.py`, `api/federation.py`) to master and worker; the
   `ui-core` edits (`pages/System.tsx`, `pages/Dashboard.tsx`) to both frontends.

### Deploy note — the panel stops dying with its neighbours (Phase 8 wave 5d)

This wave changes **no schema, no environment variable, no authorisation and no federation contract**,
and — despite being scoped as a deploy-cost wave — **adds no container and needs no `.env` edit beyond
the image pins**. It is a pull-and-restart. It changes what happens during two outages, and it accepts
one cost in exchange. Read all seven points.

1. **`.env` and `docker compose up` are unchanged. Pull the seven images and restart.** The idea that
   was priced into this wave — a local `redis` container on the sub and bot hosts — was **not taken**:
   `flask-limiter` can move its counters into the process when its storage is unreachable, which costs
   one line and buys more. So there is no new service, no new volume, no new port, no new variable, and
   nothing to do on any host except pin the new tags.

2. **What changes when the data tier's Redis is unreachable.** Before: every rate-limited route answered
   **HTTP 500**, and on the sub host that is *all four* subscription routes — client apps stopped
   receiving configuration entirely. After: the request is served and the limit is still enforced,
   counted in the process instead of in Redis. One line appears in the log
   (`Rate limit storage unreachable - falling back to in-memory storage`), and one more when it recovers
   on its own (`Rate limit storage recovered`). **This is not "limits are off":** `swallow_errors` would
   have done that, including on the admin login, and was rejected for exactly that reason. Because every
   backend runs `-w 1`, the in-process counter covers the same population the Redis one did.

   **Be precise about how far this goes, because the honest limit matters operationally.** The node
   snapshots live in that same Redis, so a user whose keys are only on nodes still gets nothing while it
   is down — 404 instead of 500. Measured on a live stand with the Redis container killed: `/info` 200,
   `/sub/<uuid>` 404, the aggregate 404, where all three were 500 before. And if the whole data-tier VM
   is down, Postgres goes with it and the sub host has nothing to answer from at all — every subscription
   route reads `TelegramUser`/`Client` before anything else. **No configuration fixes that; it needs node
   state mirrored upward, which is a separate piece of work.**

3. **A node whose own Redis has not finished starting can be linked again.** Same mechanism.
   `POST /api/federation/handshake` is the only unauthenticated route a node serves and it carries a
   limit, so a half-started node used to answer 500 to the very first thing an admin does to it. It now
   answers its own `401 no pending link token`. Point 5 of the wave-4b note is marked accordingly.

4. **What changes when the cron host is down for more than a minute.** Before: node snapshots expired
   after 60 seconds and every reader saw nothing — subscriptions answered `404 "User not found"` (which
   a client app reads as *this subscription does not exist*, not *try later*), the bot's Keys and
   Statistics screens went empty, and the master's Dashboard stopped listing node clients. After: each
   poll also stores a copy with **no TTL**, and readers fall back to it. Subscriptions keep working with
   the node servers in them, indefinitely.

   **The cost, stated plainly: a client you disable or delete during that outage goes on being served
   until the cron host comes back.** That is deliberate, and it is the same shape as two windows the
   deployment already has (the 60-second subscription cache, and the 24-hour update interval announced
   to client apps). It is why the fallback is never silent — see the next point.

5. **You will see it on the Panels page, and that is the signal to act.** A panel nothing is polling now
   shows an amber **Stale** badge instead of a green *Online*, with the time of the last successful poll
   and the line *"Nothing is polling this panel. Subscriptions and the bot are being served from the copy
   taken N minutes ago — check the cron host."* Before this wave that card said `Online` indefinitely,
   because the page fell back to a status row in Postgres that only the cron host ever writes. The serving
   host also logs one WARNING per outage naming the panel and the age of the copy. **`Stale` means "nobody
   is polling", never "the node is down"** — a node that is genuinely offline while the poller is alive
   still reads `Offline`.

6. **Two Redis keys per panel become five, and they never expire.** `panel:<id>:snapshot:last` and
   `panel:<id>:last_poll:last` are written on every poll with no TTL. They are removed when the panel is
   deleted. Sizing: one snapshot per linked panel, the same payload the live key already holds — for a
   fleet of ten nodes this is tens of kilobytes, not a capacity decision. **If you have ever removed a
   panel by deleting its row directly in Postgres rather than through the panel**, delete its keys by
   hand as well (`DEL panel:<id>:snapshot:last panel:<id>:last_poll:last`); nothing else will.

7. **Bump `master`, `worker`, `sub`, `bot_api`, `cron`, `frontend_admin` and `frontend_node` — seven
   images. `bot`, `caddy` and `xray_egress` are untouched**, including the `docker-compose.bot.yml` edit
   below, which changes the file and not the image. The `panel-core` edits (`app_base.py`,
   `extensions.py`, `services/panel_proxy.py`) fan out to all five backends; `ui-core` (`lib/types.ts`)
   and `admin` (`pages/Panels.tsx`) to both frontends. A partial rollout is harmless in both directions
   and needs no ordering: an old reader simply ignores the two new keys, and a new reader against a fleet
   whose cron host still writes only three keys behaves exactly as it does today.

   One tidy-up rides along: `RATELIMIT_STORAGE_URI` is gone from the **`bot`** service in
   `docker-compose.bot.yml`. It was a mandatory `${VAR:?}` on an aiogram poller that has no limiter and
   never read it. **Nothing to do** — keep the line in `.env.bot.example`, because `bot-api` on the same
   host does read it, and the stack still refuses to start without it for that reason.

### Deploy note — the panel starts telling you things, and two secrets become replaceable (Phase 8 wave 6)

This wave changes **no schema, no federation contract and no authorisation**. It adds one optional
environment variable and — the only real deploy cost, and the reason this is not a plain
pull-and-restart — **one volume line on the master and on every node**. Read all eight points.

1. **The one thing you must do by hand: re-run `docker compose up -d`, not just `pull`.**
   `docker-compose.master.yml` and `docker-compose.node.yml` now mount `./certs:/root/cert:ro` into
   the **backend** service. The certificates were mounted into Caddy only, so the backend could not
   see the file whose expiry it is now expected to report. If you pull the images without recreating
   the containers, everything else in this wave works and the certificate line reads *"not mounted"*
   — which is honest, and is also exactly what you will see if you forget.

2. **⚠️ The certificate line is gone again — wave 11 removed it** when Caddy took over issuance and renewal, along with the `./certs` mount into the backend that point 1 above asks for. The other four lines stand. **System → About grows five health lines.** Certificate expiry (with the SAN list and the date in
   the tooltip), the `bot_event` backlog, payments stuck in `processing` or pending over a day, the
   versions of the neighbours, and whether the data tier answers. All five are **about the host you
   are logged into**: a node's counts come from its own SQLite, not from the shared Postgres, and
   that is deliberate — an approximation of a fleet-wide number would be a worse thing to show than a
   true local one. The card never fails: any reading that cannot be taken says so in place rather
   than taking About down with it.

   **The certificate line is the half of §10.6 that this wave does do.** Issuing and renewing
   certificates is still manual on every host, still has no cron, and is still the most likely real
   outage in the deployment. Now at least the clock is visible: amber under 14 days, red past expiry.

3. **sub, bot-api and cron become visible for the first time.** Each role stamps its own version into
   the shared Redis once a minute (`panel:role:<name>:status`, 180s TTL), riding the healthcheck
   traffic every stack already generates. A host that stops reporting **disappears from the list**
   rather than showing its last known version — "reporting version X" is a claim with a timestamp,
   and a stale one is worth less than silence. **Nothing to configure**: `SHARED_REDIS_URI` is
   already mandatory everywhere. A neighbour still on an older image simply does not appear until it
   is updated, which is itself the signal.

4. **Rotating the bot service token now says so out loud.** The panel's confirmation already warned
   that the bot stops until `BOT_SERVICE_TOKEN` is updated on the bot host; what was missing was any
   sign of it **from the bot**, whose loops logged the resulting 401s at INFO next to ordinary
   network hiccups. It now logs **ERROR**, once per outage, naming the variable and the restart.
   Deliberately still no grace period: a window in which the old token keeps working is a window in
   which a leaked token keeps working.

5. **A user's subscription link can be replaced — new button, real consequences.** Bot → Users → the
   user's card → **Reset link**, behind a confirmation. The old URL dies immediately, including in
   the user's own app, which will fail its next update until the new link is imported. The bot sends
   them the new one as soon as the reset lands. Their keys, expiry and access are untouched. Use it
   when a link has leaked; there is no undo and no way back to the old value.

   This adds one bot text (`notification.sub_link_reset`) and one event type. **`CURRENT_BOT_TEXTS_VERSION`
   does not move** — a purely additive key arrives through the ordinary seed — so **no force-reseed
   happens and no customised text is touched**.

6. **A payment stranded in `processing` is recovered in minutes instead of a day and a half.** If a
   process died between the atomic claim and the end of provisioning, the row sat in a status the
   paid path did not look for. `poll_pending_payments` now releases such a row back to `pending`
   after two minutes and lets the ordinary path have it. **The claim itself is unchanged**, which is
   the point: widening it to accept `processing` would close the same gap and reopen the double-grant
   it exists to prevent. Nothing to do; if you have such rows today they will drain on the first
   poll after the update.

7. **Federating over a private network is possible now, and still off by default.**
   `FEDERATION_ALLOW_PRIVATE_URLS=true` on the master lets you add or relink a panel whose URL
   resolves to a private, loopback or `.internal` address. **Leave it unset on a public deployment**
   — the check is what stops that endpoint from fetching arbitrary internal URLs, and the flag
   relaxes it for every panel at once. Only `1`/`true`/`yes`/`on` counts, so the empty placeholder in
   `.env.master.example` changes nothing.

8. **Bump `master`, `worker`, `sub`, `bot_api`, `cron`, `frontend_admin`, `frontend_node` and `bot`
   — eight images. `caddy` and `xray_egress` are untouched.** The `panel-core` edits (`app_base.py`,
   `services/{panel_proxy,role_status,health}.py`, `data/bot_texts_defaults.yaml`) fan out to all
   five backends; `panel-adminapi` (`api/system.py`) to master and worker; `panel-master`
   (`api/{panels,bot_admin}.py`) and `panel-botapi` (`jobs/payments.py`) to their own; `ui-core` to
   both frontends; `sub-page` to the `sub` image; `tg_bot` to the bot. Deploy order does not matter
   and a partial rollout degrades quietly: an old master shows no health card, a new master shows a
   neighbour as absent until that neighbour is updated, and an old bot simply never renders the
   reset-link notification.

### Deploy note — the data tier gains a third credential, and two things that were reporting nothing start reporting (Phase 8 wave 7)

This wave changes **no schema and no authorisation**. It adds one field to the federation snapshot
(additive), and — the part that makes this more than a pull — **a new Redis ACL user on the data
tier and a new variable on the bot host**. Read all eight points.

1. **Do the data tier first, and set `REDIS_BOT_PASSWORD` before you touch it.** `docker-compose.postgres.yml`
   now creates a third ACL user, `bot`, alongside `panel` and `node`, and demands the password
   through `${REDIS_BOT_PASSWORD:?}` — the stack will not come up without it. Then on the bot host
   set `BOT_SHARED_REDIS_URI=redis://bot:<that password>@<data-vm>:6379/0`. **If you update the bot
   host before the data tier, the poller cannot authenticate and receives no events at all** — no
   payment confirmations, no expiry warnings, nothing, and it says so only at INFO. Order: data
   tier, then bot host.

2. **Why the bot gets its own credential.** It held `panel`, which reads every node snapshot — that
   is every user's UUID, telegram_id, e-mail, traffic and expiry — and can write anything into
   `bot:events`. The aiogram poller is the one process in the deployment that feeds untrusted
   internet input to something other than a web framework, and all it needs from the bus is
   `SUBSCRIBE` to one channel. bot-api keeps `panel`; it publishes and reads snapshots. Verified on
   a live Redis with the new ACL: subscribe to `bot:events` works, reading a snapshot, publishing,
   `SETEX` and subscribing to `panel:refresh` all answer NOPERM.

3. **`PANEL_SECRET_PATH` is no longer handed to the master's backend.** `api/federation.py` — the
   only backend module that reads it — is node-only now (point 5), so on the master the variable is
   read by the frontend entrypoint and by Caddy and by nothing in the backend image. **Nothing to
   do:** keep it in `.env.master.example`, those two still need it. It is unchanged and still
   mandatory on the node.

4. **System → About and the Panels page start showing what they could not.** Each node's release now
   travels in its federation snapshot and appears on its card in Panels. This replaces a wave-6
   mechanism that could never have worked for nodes: their data-tier credential forbids `SETEX`, so
   the stamp failed silently on every installation, and the key was shared by every node anyway, so
   three nodes would have overwritten each other once a minute. **If you rolled out wave 6 and
   wondered why no node ever showed a version — that is why, and this fixes it.** A node still on an
   older image shows no version until it is updated, which is itself the signal.

5. **`GET /api/backup` and the federation server are no longer in the master image at all.** They
   were unregistered but installed, so one `register_blueprint` line stood between the master and
   (a) being linkable as somebody's node and (b) handing out its own database over an admin JWT —
   verified by adding the line, which answered 200 to both. Packaging refuses now instead of the
   factory remembering to. **No behaviour changes**: the master answered 404 on both before and
   answers 404 on both after.

6. **The subscription page and the admin panel stop disagreeing about whether a node is up.** They
   read the same marker now. With the cron host down, the admin sees an amber `Stale` card (wave 5d)
   and the user's page keeps showing their nodes as up, which is the honest split: "we stopped
   polling" is something an admin can act on and a user cannot. Before, the page read a Postgres
   column that is written only on a change, so it said `online` forever.

7. **Two admin routes are gone: `GET /api/panels/<id>/system-stats` and `POST /api/panels/<id>/restart`.**
   Neither had a caller in any bundle; restarting a node's Xray from the master goes through
   `POST /api/restart?panel_id=` and has since wave 5c. **If you have a home-grown script calling
   either, it gets a 404** — that is the only outward-facing removal in this wave. The *Test
   connection* button on a panel card still works and now reports the node's own words, but it no
   longer writes the stored status: it asks the cron host to re-poll, the same way *Relink* does.

8. **Bump `master`, `worker`, `sub`, `bot_api`, `cron`, `frontend_admin` and `frontend_node` — seven
   images. `bot` and `caddy` are untouched** (the bot's change is a compose mapping, not code). A
   partial rollout degrades quietly in both directions: an old master shows no node versions, a new
   master shows none until the nodes are updated, and nothing is corrupted either way.

### Deploy note — what the first full run of the stand found, and what it cost to fix (Phase 8 wave 8)

This wave changes **no schema, no federation contract and no authorisation**. It comes out of the
first end-to-end run of the whole deployment on four machines, so every item below was observed
happening rather than reasoned about. Two things need a deployer's attention — one new variable on
the node and one behaviour every user will see. Read all seven points.

1. **`PROXY_DOMAIN` is now handed to the node's *backend*, not only to its Caddy.** It is already
   mandatory on that host, so `.env` needs no edit — but `docker compose up -d` must recreate the
   container, a `pull` alone will not. The backend reads it to refuse a REALITY inbound whose SNI
   the reverse proxy will not route to Xray: on `:443` Caddy decides by SNI, learns the decoy name
   from `PROXY_DOMAIN`, and the inbound's `serverNames` is what the client presents. When the two
   drifted apart every client was quietly handed to the panel instead of Xray and simply never
   connected, with nothing anywhere reporting a fault. Saving such an inbound now fails with a
   message naming both values.

2. **An expired subscription answers `200` with an explanation instead of `404`.** A client app
   renders a 404 as a failed update, so the one screen a user looks at when the VPN stops working
   could not distinguish "your subscription ran out" from "this link was reset" (a button since
   wave 6) or "you mistyped the URL". The reply is now a config the app accepts, carrying a single
   entry named `⛔ Подписка закончилась — продлите в @<bot>` and pointing at `127.0.0.1:1` so a tap
   fails instantly rather than hanging, with a random UUID so a leaked link hands out no credential.
   `Subscription-Userinfo: expire=` carries the real past date, which several clients render as
   "expired" natively. The same applies to a blocked account and to a per-key link.
   **An unknown token still answers 404, deliberately** — otherwise revoking a leaked link would look
   exactly like an expiry, and probing random tokens would get a meaningful reply. The handle in that
   message comes from the bot itself: it reports its username on the 60-second runtime-config poll
   (`X-Bot-Username`), so a fresh deployment shows "продлите в боте" until the bot has polled once.

3. **`panel:refresh` was losing about half its messages on every installation, and the fix is one
   argument.** `redis-py 8` changed the default `socket_timeout` from "block forever" to 5 seconds;
   `new_shared_redis_subscriber` relied on the old default, so a quiet channel raised `TimeoutError`,
   the listener treated that as a dropped connection and slept 5 seconds before resubscribing.
   Measured on the stand: 5 of 12 nudges produced an out-of-band poll, 7 were lost, and the cron host
   logged two lines every ten seconds forever. Nothing looked broken because the ordinary 10-second
   poll is the safety net — an admin action simply took up to 10 s to appear instead of half a second.

4. **A data tier that is unreachable now fails fast instead of slowly.** Two independent causes were
   measured: no `connect_timeout` reached libpq, so a *network-level* outage (VM off, partition,
   firewall — the failure a separate VM exists to survive) made requests hang with no answer at all
   rather than erroring; and the Redis clients remembered nothing, so every request re-tried every
   lookup, turning a 0.5 s subscription answer into 4-10 s and the master's own pages into 2.7-3.5 s.
   Postgres now gets `connect_timeout=5` via `engine_options`, and both Redis clients skip a tier
   known to be down for 10 seconds. **Nothing to configure.** Note the shape of the failure that is
   *not* affected: a Postgres process that dies while its host stays up was always refused instantly.

5. **System → About distinguishes "this host died" from "this host was never deployed".** Wave 6 made
   a role disappear from the card when it stopped reporting, which is right for the *version* — a
   stale number is a claim with no timestamp — but absence then meant two opposite things at once.
   For the bot host those are different emergencies: while it is down **no payment is confirmed at
   all**. Each role now also writes a TTL-less copy, and a host that has gone quiet shows an amber
   *not answering · last seen N ago* instead of vanishing. Observed by accident when the stand's bot
   host rebooted itself mid-session.

6. **Three smaller things a deployer will notice.** Every `.env.*.example` now carries
   `&sslrootcert=/etc/ssl/certs/ca-certificates.crt` on `DATABASE_URL` — without it **no role can
   connect at all** (libpq looks for `~/.postgresql/root.crt`, which no container has, and
   `sslrootcert=system` does not help because `psycopg2-binary` bundles its own OpenSSL), which means
   the shipped instructions had been unusable. `XRAY_IMAGE` is `:latest`, because that registry
   publishes no version tags and `xray_core_ref` is a *source* ref for the worker's protobuf stubs.
   And the node's `xray-core` restarting a few times on the very first `up` is expected and now
   documented: the panel writes the config at boot, and gating Xray behind the backend's healthcheck
   would add real downtime to every later restart to silence a message that appears once.

7. **Bump `master`, `worker`, `sub`, `bot_api`, `cron`, `frontend_admin`, `frontend_node` and `bot`
   — eight images. `caddy` and `xray_egress` are untouched.** The `panel-core` edits
   (`extensions.py`, `db_config.py`, `services/{reality_health,bot_status,role_status,panel_proxy}.py`,
   `data/bot_texts_defaults.yaml`) fan out to all five backends; `panel-adminapi`
   (`api/{inbound,system}.py`) to master and worker; `panel-master`, `panel-sub`, `panel-botapi` and
   `panel-worker` to their own; `ui-core` and `admin` to both frontends; `tg_bot` to the bot. One new
   bot text (`checkout.creating`) arrives through the ordinary additive seed, so
   `CURRENT_BOT_TEXTS_VERSION` does **not** move and no customised text is touched. A partial rollout
   degrades quietly in both directions.

### Deploy note — a paid key that never reached Xray now repairs itself, and three settings get a form (Phase 8 wave 9)

This wave bumps `CURRENT_DB_VERSION` **25 → 26**, changes **no authorisation and no environment
variable**, and removes one field from the federation snapshot that nothing read. It is a
pull-and-restart, with one ordering rule that already exists. Read all seven points.

1. **The deploy order is the one you already have, and it matters here: data tier → cron → master,
   sub, bot-api, nodes anywhere after the data tier.** The new column arrives on the shared Postgres
   from the cron service and on a node's SQLite from the node itself. Nothing to do by hand.

2. **What was broken: a payment could be confirmed for a key that never reached the running Xray.**
   A node writes the client and its idempotency receipt in one transaction and *then* materialises
   the grant — regenerating the config, adding the user over gRPC or restarting the core. If that
   second half failed (Xray down, file-lock contention, a gRPC error), the row existed, the receipt
   existed, and the 30-second retry from the bot host took the receipt's fast path: it replayed the
   stored answer and never synchronised. The payment went `succeeded`, the user was told his access
   was granted, and he could not connect until some *other* change on that node happened to
   regenerate the config. The receipt now carries whether the grant was materialised; a replay that
   finds it unmaterialised synchronises first and answers only then, and if that fails too the
   payment stays `pending` for the next poll. **Existing receipts are read as unmaterialised**, so
   the first retry against an old row costs one extra config regeneration — deliberately, since the
   opposite error leaves somebody without a working key.

3. **Postgres migrations can add columns now, which changes how you write the next change.** Until
   this wave `migrate_postgres_db` was `create_all` plus a few statements and owned no
   `ALTER TABLE`, so a new column reached a live shared database never — and the last two waves
   chose table-shaped changes to route around it. `_add_missing_columns` diffs the models against
   `information_schema` and adds what is missing. Two limits stay on purpose: **nothing is ever
   dropped** (a column in the database but not in the models is left alone), and a `NOT NULL` column
   with no `server_default` is added *nullable* with a WARNING rather than failing the cron
   service's start-up. **Give every new column a `server_default` if it must be `NOT NULL`.**

4. **Three tables leave the shared Postgres — but only if they are empty.** `traffic_snapshot`,
   `domain_stat` and `notification_log` are written only by node jobs, into each node's own SQLite;
   on the shared database they have had no reader at all. They are now excluded from creation and
   dropped by the **cron service only**. **An installation carried over from the monolith may hold
   real rows there** — traffic history and sent-notification marks. A non-empty table is *not*
   dropped: it survives, and the cron service logs its name and row count at start-up. If you want
   that history gone, drop it by hand after checking it; if you want it kept, do nothing and it
   stays. Nodes are unaffected — a worker pointed at Postgres (the compose file omits `DATABASE_URL`
   rather than forbidding it) never asks for the drop, because the flag is set at the cron role's
   call site.

5. **Three settings that could only be changed with SQL now have a form: Bot → Settings →
   "Branding · this panel".** The **brand name** titles the subscription inside the user's client
   app and on the subscription page (until now every deployment showed the built-in "Подписка" /
   "Subscription"); the **config update interval** is the `Profile-Update-Interval` a client app
   obeys (default 24 h); the **panel name** is how this master introduces itself when linking a
   node, shown on that node's System → Link card (until now every master was "Master"). Nothing
   changes until you set them — the defaults are exactly what was hard-coded before.

6. **A node stops sending its own name, and this is additive in the direction that matters.** It
   used to put `panel_name` in every snapshot and `name` in its handshake reply; the master
   discarded both and shows `LinkedPanel.name`, typed by whoever added the panel. A new master
   against an old node ignores the extra field, an old master against a new node was already
   ignoring it. **No panel's displayed name changes.**

7. **Bump `master`, `worker`, `sub`, `bot_api`, `cron`, `frontend_admin` and `frontend_node` —
   seven images. `bot`, `caddy` and `xray_egress` are untouched.** The `panel-core` edits
   (`models.py`, `db_migration.py`, `pg_migrate.py`, `app_base.py`, `services/provisioning.py`) fan
   out to all five backends; `panel-master` (`api/bot_admin.py`), `panel-worker`
   (`api/federation.py`) and `panel-cron` (`roles/cron.py`) to their own; `ui-core` (`lib/types.ts`)
   and `admin` (`components/bot/SettingsTab.tsx`) to both frontends. No bot text is added or
   removed, so `CURRENT_BOT_TEXTS_VERSION` does not move.

### Deploy note — Caddy issues its own certificates, and the manual renewal goes away (Phase 8 wave 11)

This wave changes **no schema, no federation contract and no authorisation**. It removes the only
recurring manual operation left in the deployment, and with it a card in the panel. Read all six
points — two of them are things you must not do afterwards.

1. **`./certs` stops being used, and the mount is gone from six places.** Caddy obtains and renews
   every certificate itself over ACME; `tls.automation.policies` replaced `load_files`. **Existing
   pairs in `./certs` are simply ignored** — nothing reads them, nothing deletes them, and you can
   remove the directory once the new stack is up. On the first `up` after the update each host asks
   Let's Encrypt for its own name, which takes a few seconds; until then it serves nothing on `:443`.

2. **`:80` must be reachable from the internet on master, node, sub and bot.** It was a redirect
   before and could be firewalled without anyone noticing; it now carries the HTTP-01 challenge, and
   it is the *only* path — layer4 owns `:443` for SNI routing, so TLS-ALPN is unavailable. A cloud
   firewall closing 80 means no certificate at all. The domain must also resolve to that box, which
   was already true for the panel to work.

3. **Do not delete `caddy_data` on upgrade.** It holds the ACME account and every issued
   certificate. It existed on all four hosts already, and was nearly empty; from now on losing it
   forces a re-issue, and Let's Encrypt allows **5 identical certificates per week**. A rebuild loop
   that recreates volumes will exhaust that in an afternoon and leave the host without TLS until the
   window rolls.

4. **A node asks only for its `PANEL_DOMAIN`, never for `PROXY_DOMAIN`.** The decoy is a raw
   passthrough whose SNI is somebody else's domain, so requesting it could only fail — and LE counts
   *failed* validations against the account, which would eventually block the certificate the node
   actually needs. Guarded in `caddy/caddygen/generate_test.go`.

5. **System → About loses its certificate line.** Caddy owns the expiry and stores the file where
   the backend cannot read it, so the only reading left would be "not mounted" forever on a healthy
   host. **The warning it provided is not replaced by nothing:** set `ACME_EMAIL` and Let's Encrypt
   mails you before a certificate lapses. `ACME_CA` points at a different directory — use the LE
   **staging** URL while rehearsing a deploy so a debugging session does not spend the weekly limit.
   Both are optional and new on the four TLS hosts.

6. **A local deployment works without a public name.** `panel.local`, `localhost`, a bare IP or any
   name without a dot transparently gets Caddy's internal CA instead of ACME — which is also what
   replaced `scripts/generate_local_cert.sh`, deleted in wave 10.

   Bump `caddy`, plus `master`, `worker`, `sub`, `bot_api`, `cron`, `frontend_admin` and
   `frontend_node` — eight images. `bot` and `xray_egress` are untouched. The `caddy` edit is
   `caddygen/{generate,config}.go`; the `panel-core` edit (`services/health.py`) fans out to all five
   backends; `ui-core` (`lib/version.ts`, `pages/System.tsx`) to both frontends. A partial rollout is
   safe in one direction only: an old Caddy image with the new compose file finds no `./certs` mount
   and crash-loops, so update the image and the compose file together on each host.

## Configuration

**There is no shared `.env.example`.** Each host copies its own: `.env.master.example`, `.env.node.example`, `.env.sub.example`, `.env.bot.example`, `.env.cron.example`, `.env.data.example` → `.env` on that box and nowhere else. (Wave 1 created five of those and wave 2's cron host added the sixth; the wave-1 note below still names five, which is what was true when it was written.) One file could not be correct for every host even in principle — `RATELIMIT_STORAGE_URI` must point at the box's *own* Redis on the master and on a node and at the *data tier* on the sub and bot hosts, two mutually exclusive values of one variable, which the old single file carried at once (one live, one commented out) and expected the deployer to reconcile by hand. Each file now holds only what its host reads, with no commented alternatives. `backend/tests/test_env_examples.py` enforces both directions: every `${VAR:?…}` a compose file demands is defined in that host's example, and no example defines a variable its own compose file never references. Key variables:
- `PANEL_DOMAIN`, `PANEL_SECRET_PATH` — routing/TLS. `PANEL_DOMAIN` is **per-host by design**: on a node it must be *that node's* domain, because `services/notifications.py` also uses it as the node's identity in bot events. On the **sub and bot hosts it routes nothing** — neither box serves it — and it is read there only as the "is this a real deployment" marker (`app_base` refuses a weak `SECRET_KEY`, `db_config` refuses a `DATABASE_URL` without `sslmode=verify-full`), plus, on sub, as `api/subscription.py`'s default server address for an inbound with no explicit host. `PANEL_SECRET_PATH` is read by exactly one module, `api/federation.py`, which ships from `panel-adminapi` — so it belongs on the **master and node only**. Wave 5a removed it from `docker-compose.{sub,bot}.yml` and from their examples, where it had been a mandatory `${VAR:?}` that no code in either image could read. `tests/test_env_reaches_code_that_reads_it.py` resolves each service to its image's dependency closure and fails on any variable handed to a container that nothing inside it mentions.
- `PROXY_DOMAIN` — decoy SNI, raw-TCP passthrough to Xray (masquerade). **Node-only.** The master has had no `xray` service since phase 3b, so `docker-compose.master.yml` no longer names it at all. Since wave 8 it is handed to the node's **backend** as well as its Caddy: `api/inbound.py` refuses a REALITY inbound on `:443` whose `realitySNI` differs from it, because the two are one value stored in two places and a mismatch routes every client into the panel instead of Xray without reporting anything.
- **Egress host state is not in `.env`, and that is a constraint rather than a preference.** A node carrying dedicated outgoing addresses grows two files beside its `.env` — `egress.conf` (`EGRESS_UPLINK_IFACE`, optional `EGRESS_PLAN_URL`) and `egress-owned` (the addresses the synchroniser raised itself) — plus `egress-sync.sh` and a `panel-egress-sync.timer`. They stay out of `.env` because `tests/test_env_examples.py` forbids a variable in a host's example file that its own compose file never references, and no container reads either of them. The presence of `egress.conf` is also what makes `install.sh` pass `--profile egress` to every compose call on that host, so `update` and `doctor` keep seeing the sidecar; `COMPOSE_PROFILES` in `.env` would trip the same guard. The synchroniser reads `GET /api/system/egress/host-plan` (egress token, node-only in practice) and owns exactly three things: the addresses listed in `egress-owned`, the `nat` chain `EGRESS_SNAT`, and routing tables `100–199` with `ip rule` priorities `30000–30099`. Three behaviours are load-bearing and each has a test: **an unreachable panel leaves the host untouched (exit 1) while an empty plan clears it (exit 0)** — collapsing those two drops every customer's address on a reboot where the backend starts a second later than the timer; **a settled host issues no mutating command and prints nothing under `--dry-run`**, which is what lets `install.sh doctor` tell a converged host from a stalled one; and **the jump into `EGRESS_SNAT` is re-asserted as *first* in `POSTROUTING`, not merely present**, because Docker rewrites that chain on `docker network create` and on daemon restart, and its MASQUERADE landing ahead of ours sends every dedicated address out on the primary IP while the chain, the aliases and the routing all still look correct.
- `SUB_DOMAIN` *(required — subscriptions do not work without it)* — the dedicated subscription domain, and since phase 8 wave 3b the **only** host any subscription link can name: `https://<SUB_DOMAIN>/api/sub/u/<token>` for a Telegram user, `https://<SUB_DOMAIN>/api/sub/<uuid>` for a single key. Must be in the cert's SAN and in the backend container's env. The old `PANEL_DOMAIN` + secret-path fallback is gone: it named the master, which no longer serves the route at all, so it turned an empty variable into a link that 404s in a browser while client apps kept working — quiet enough to ship. `build_aggregate_sub_url` / `build_client_sub_url` now return `None` instead. **All four service hosts demand it via `:?`, and only one of them serves it:** the sub host answers the routes; the master and each node read it purely to build the links their own Dashboard hands out (`api/inbound.py` → `sub_url` per client); bot-api reads it to build every link the bot sends a user (`GET /bot-service/users/<id>/state`). A host knowing its own domain is not enough — nothing asks the sub host what it is called.
- `SECRET_KEY`, `PANEL_ADMIN_USER`, `PANEL_ADMIN_PASSWORD`.
- `XRAY_CORE_REF` — Xray-core version to compile into the **worker** image (`backend/Dockerfile.worker`'s build-arg) — the only one of the five per-role backend images that carries the Xray runtime (build-time only).
- `RATELIMIT_STORAGE_URI` — **this box's own** Redis: rate limiting, plus this role's own subscription-response cache. On the master and on a node that is the stack's private `redis` container; on sub and bot-api, which run no Redis of their own, it points at the data tier — which is why an unreachable value there used to 500 every subscription request and no longer does (see Docker Services). Read in exactly three places — `app_base` puts it into `app.config["RATELIMIT_STORAGE_URI"]` for Flask-Limiter, the start-up check beside it, and `sub_cache` — and `tests/test_redis_split.py` fails on a fourth, whether it names the variable or calls `extensions.local_redis_uri()`. It is **not** required by the `bot` container: that is an aiogram poller with no limiter, and until wave 5d `docker-compose.bot.yml` demanded it there through a `${VAR:?}` anyway (§87). It stays in `.env.bot.example`, because `bot-api` on the same host does read it. **Only the sub role ever populates that cache**: `sub_cache.get`/`set` are called from `api/subscription.py` alone, which since wave 3b is registered nowhere else. The master and each node only ever `DELETE` from it after an edit, so describing their local Redis as holding "the sub-cache this host reads" — as `.env.{master,node}.example` did until wave 5a — is wrong in a way that invites a deployer to point it somewhere shared.
- `PANEL_SECRET_PATH` — routing. **Not handed to the master's backend since wave 7**: the one backend module that reads it, `api/federation.py`, became node-only, so on the master it is read by the frontend entrypoint and by Caddy's route and by nothing in `panel-master`. It stays in `.env.master.example` for those two, and is still a mandatory `${VAR:?}` on the node's backend.
- `FEDERATION_ALLOW_PRIVATE_URLS` *(master only, default off)* — opens `_validate_panel_url` to private, loopback, link-local and `.internal` panel addresses. Off, the master refuses to add or relink such a panel, which is what stops `POST /api/panels` from being a request forwarder into the private network; on, "the master and its nodes share a private segment" — a legitimate topology this product could not express from the UI at all, and which blocked this repo's own live stands in waves 5c and 5d — becomes possible. It relaxes the check for **every** panel, not one, and only `1`/`true`/`yes`/`on` counts as consent, so a placeholder left empty in `.env` does not silently open it.
- `BOT_SHARED_REDIS_URI` *(bot host only)* — the same bus as `SHARED_REDIS_URI`, through the subscribe-only `bot` credential. `docker-compose.bot.yml` maps it into the poller's `SHARED_REDIS_URI`, so no code in `tg_bot` changed; bot-api keeps the `panel` one on the same host, because it publishes and reads node snapshots while the bot only listens. Two containers, two credentials, because they need two different things.
- `REDIS_BOT_PASSWORD` *(data tier)* — the password behind it; the ACL user is created by `docker-compose.postgres.yml`'s entrypoint alongside `panel` and `node`.
- `SHARED_REDIS_URI` — the **data-tier** Redis (`redis://` or `rediss://`), carrying the `bot:events` bus, the node snapshots (live **and** last-known — five keys per panel since wave 5d) and the `panel:refresh` nudge. **Required via `:?` on all five service hosts** — master, every node, sub, bot and cron. It replaced `BOT_EVENTS_REDIS_URI`, which defaulted to `RATELIMIT_STORAGE_URI`; that default is gone deliberately, see the two-Redis paragraph under Docker Services. Use `redis://node:<REDIS_NODE_PASSWORD>@<data-vm>:6379/0` on a node (publish-only credential) and `redis://panel:<REDIS_PANEL_PASSWORD>@<data-vm>:6379/0` everywhere else. The bus crosses hosts and carries the ACL password plus `telegram_id`/`email` in cleartext — run it over a private network between hosts or over `rediss://`.
- `BACKEND_LOG_LEVEL` *(default INFO)* — backend log verbosity. Every API request (`app.requests`), scheduler job run with duration (`app.jobs`), and federation HTTP call is logged at INFO/DEBUG; `DEBUG` additionally echoes every SQL statement (`sqlalchemy.engine` + per-statement timings in `app.sql`). Slow thresholds: `BACKEND_SLOW_SQL_MS` (default 200) and `BACKEND_SLOW_REQUEST_MS` (default 1000) promote slow statements/requests to WARNING. The backend container has json-file log rotation (50 MB × 5).
- `POSTGRES_BIND` / `REDIS_BIND` *(data tier)* — which host interface each port is published on. Both **default to `127.0.0.1`**, i.e. closed, so an unset value cannot publish the data tier to the internet; set them to the data VM's private-network address. Postgres is reasonably covered even when exposed (`ssl=on`, `scram-sha-256`, clients required to use `sslmode=verify-full`); **the Redis is not — it runs with no TLS at all**, so its ACL password and every `bot:events` payload (`telegram_id`, client e-mails) would cross the wire in clear.
- `*_IMAGE` — per-service image pins (mirrors `versions.json`). The backend is now five images, each pinned by its own variable: `MASTER_IMAGE` (`docker-compose.master.yml`), `WORKER_IMAGE` (`docker-compose.node.yml`), `SUB_IMAGE` (`docker-compose.sub.yml`), `BOT_API_IMAGE` (`docker-compose.bot.yml`), `CRON_IMAGE` (`docker-compose.cron.yml`) — `BACKEND_IMAGE` does not exist anywhere in the repo any more, since wave 10 deleted the frozen monolithic compose files that were its last holders. The frontend is likewise two images: `FRONTEND_ADMIN_IMAGE` (`docker-compose.master.yml`) serves the admin SPA, `FRONTEND_NODE_IMAGE` (`docker-compose.node.yml`) serves the node SPA — `FRONTEND_IMAGE` is gone the same way. See the deploy notes below.

Bot configuration is **not** in `.env`. It lives in `SystemSetting` rows managed via **Bot → Settings** in the panel UI: `bot_token`, `admin_telegram_ids`, `bot_service_token`, YooKassa `shop_id` / `secret_key`, `display_timezone`, and — since wave 9 — `brand_name`, `subscription_update_interval_hours` and `panel_name`. The last three are not the bot's at all; they are the panel's, and they live on that form because it is the master's only settings surface that writes to the shared Postgres (`/api/system/settings` is the *node's* Xray settings and answers 501 on a master). `brand_name` and `subscription_update_interval_hours` are read by the **sub** role — the subscription's title in a client app and its `Profile-Update-Interval`; `panel_name` is read on the **master** only, as the name it introduces itself with when linking a node (`panels.py` → `master_name` → the node's System → Link card). A node no longer has a `panel_name` of its own: it used to put one in its snapshot and its handshake reply, and the master discarded both. The bot container only needs two env vars: `BACKEND_API_URL` and `BOT_SERVICE_TOKEN`. Changes take effect within ~60s without restarting the bot.

**Local vs. production validation:** When `PANEL_DOMAIN` is a local hostname (`localhost`, `*.local`, or an IP literal), the app relaxes requirements: weak `SECRET_KEY` is allowed, default `admin:admin` credentials are allowed, `memory://` rate limiting is allowed. For any real domain, all three are enforced on startup and the app refuses to start if they fail.
