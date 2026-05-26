# Multi-Panel Federation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the old Node system with a panel federation — master panel discovers, displays, and manages inbounds/users on child panels via a new federation API.

**Architecture:** Hybrid approach — new `GET /api/federation/snapshot` for efficient polling, existing inbound/user CRUD endpoints for writes (with new `federation_token_required` auth), and a proxy layer on master that routes writes to the correct panel. Redis caches child panel snapshots. Old Node model, node_sync service, and related columns/tables are fully removed.

**Tech Stack:** Python/Flask, SQLAlchemy, SQLite, Redis, gevent, React/TypeScript, Zustand, React Query, Framer Motion

**Spec:** `docs/superpowers/specs/2026-05-25-multi-panel-federation-design.md`

---

## File Structure

### Backend — New Files

| File | Responsibility |
|---|---|
| `backend/app/models.py` | Add `LinkedPanel`, `FederationConfig` models; add `TariffItem.panel_id`; remove `Node`, `NodeClientTraffic`, dead columns |
| `backend/app/utils.py` | Add `federation_token_required`, `admin_or_federation_token_required` decorators |
| `backend/app/api/federation.py` | **New** — child-side endpoints: link-token, handshake, snapshot, provision |
| `backend/app/api/panels.py` | **New** — master-side CRUD: list/create/update/delete/test panels |
| `backend/app/services/panel_proxy.py` | **New** — `FederationClient` HTTP client + proxy functions + cache refresh |
| `backend/app/jobs/panels.py` | **New** — `poll_linked_panels` scheduler job |
| `backend/scripts/migrate_v14_to_v15.py` | **New** — manual migration script for existing installs |

### Backend — Modified Files

| File | Changes |
|---|---|
| `backend/app/__init__.py` | Replace node blueprint + 4 node jobs with panels/federation blueprints + `poll_linked_panels` job |
| `backend/app/api/inbound.py` | Remove all `sync_user_*` / `sync_inbound_*` calls; add `panel_id` routing to proxy layer |
| `backend/app/services/provisioning.py` | Remove `node_sync` import; route by `TariffItem.panel_id`; remove `allowed_node_groups` usage |
| `backend/app/services/stats.py` | Remove `NodeClientTraffic` import and `_global_node_usage_map`; simplify `check_limits` |
| `backend/app/api/subscription.py` | Remove `_master_visible_to_client`, node aggregation, `NodeClientTraffic` traffic summing |
| `backend/app/jobs/notifications.py` | Read child panel client data from Redis cache for expiry/traffic notifications |
| `backend/app/jobs/billing.py` | Route `auto_renew_free_users` through proxy for remote TariffItems |
| `backend/db_migration.py` | Bump to v15; new tables for fresh installs; drop old node tables |

### Backend — Deleted Files

| File | Reason |
|---|---|
| `backend/app/services/node_sync.py` | Entire old sync system replaced |
| `backend/app/api/nodes.py` | Replaced by `panels.py` |

### Frontend — New Files

| File | Responsibility |
|---|---|
| `frontend/src/pages/Panels.tsx` | **New** — replaces `Nodes.tsx`; panel cards, add/edit/unlink, link token generation |

### Frontend — Modified Files

| File | Changes |
|---|---|
| `frontend/src/lib/types.ts` | Remove `Node`; add `LinkedPanel`, `FederationConfig`; extend `Inbound` with `panel_id`/`panel_name`; extend `TariffItem` with `panel_id`; remove `global_limit_bytes`/`allowed_node_groups` from `Client` |
| `frontend/src/pages/Dashboard.tsx` | Add panel filter dropdown; add panel badge on inbound cards; pass `panel_id` in mutations |
| `frontend/src/components/inbound/InboundForm.tsx` | Add "Target panel" dropdown when creating |
| `frontend/src/components/inbound/UserForm.tsx` | Pass `panel_id` through to API calls |
| `frontend/src/components/bot/TariffDrawer.tsx` | Two-step selection: panel → inbound; `panel_id` on `FormItem`; remove `allowed_node_groups` |
| `frontend/src/components/bot/TariffsTab.tsx` | Fetch panels instead of nodes; pass panels to drawer |
| `frontend/src/components/layout/Sidebar.tsx` | Rename "Nodes" → "Panels" |
| `frontend/src/App.tsx` | Rename route `/nodes` → `/panels`; import `Panels` page |

### Frontend — Deleted Files

| File | Reason |
|---|---|
| `frontend/src/pages/Nodes.tsx` | Replaced by `Panels.tsx` |

---

## Phase 1: Backend Foundation

### Task 1: Data Models

**Files:**
- Modify: `backend/app/models.py`
- Test: `backend/tests/test_models_federation.py`

- [ ] **Step 1: Write tests for new models**

```python
# backend/tests/test_models_federation.py
import time
import pytest
from app.models import LinkedPanel, FederationConfig


def test_linked_panel_creation(app, db):
    panel = LinkedPanel(
        name="DE-1",
        url="https://de1.example.com/secret",
        federation_token="test-token-abc",
        created_at=int(time.time() * 1000),
    )
    db.session.add(panel)
    db.session.commit()
    assert panel.id is not None
    assert panel.status == "unknown"
    assert panel.enable is True


def test_linked_panel_name_unique(app, db):
    now = int(time.time() * 1000)
    db.session.add(LinkedPanel(name="DE-1", url="https://a.com", federation_token="t1", created_at=now))
    db.session.commit()
    db.session.add(LinkedPanel(name="DE-1", url="https://b.com", federation_token="t2", created_at=now))
    with pytest.raises(Exception):
        db.session.commit()


def test_linked_panel_to_dict_masks_token(app, db):
    panel = LinkedPanel(
        name="US-1", url="https://us.example.com", federation_token="secret-token",
        created_at=int(time.time() * 1000),
    )
    db.session.add(panel)
    db.session.commit()
    d = panel.to_dict()
    assert d["federation_token"] == "••••••••"
    d_full = panel.to_dict(mask_token=False)
    assert d_full["federation_token"] == "secret-token"


def test_federation_config_singleton(app, db):
    cfg = FederationConfig.query.get(1)
    assert cfg is not None
    assert cfg.link_token is None
    assert cfg.linked_at is None


def test_federation_config_rejects_second_row(app, db):
    from sqlalchemy.exc import IntegrityError
    db.session.add(FederationConfig(id=2))
    with pytest.raises(IntegrityError):
        db.session.commit()
```

- [ ] **Step 2: Run tests — expect FAIL (models don't exist yet)**

```bash
cd backend && python -m pytest tests/test_models_federation.py -v
```

- [ ] **Step 3: Add LinkedPanel and FederationConfig models**

In `backend/app/models.py`, after the existing model classes and before the `Node` class (line ~142), add:

```python
class LinkedPanel(db.Model):
    __tablename__ = "linked_panel"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    url = db.Column(db.String(255), nullable=False)
    federation_token = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), default="unknown", nullable=False)
    last_poll = db.Column(db.BigInteger, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    enable = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.BigInteger, nullable=False)

    def to_dict(self, mask_token=True):
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "federation_token": "••••••••" if mask_token else self.federation_token,
            "status": self.status,
            "last_poll": self.last_poll,
            "last_error": self.last_error,
            "enable": bool(self.enable),
            "created_at": self.created_at,
        }


class FederationConfig(db.Model):
    __tablename__ = "federation_config"

    id = db.Column(db.Integer, primary_key=True)
    master_url = db.Column(db.String(255), nullable=True)
    master_name = db.Column(db.String(100), nullable=True)
    federation_token = db.Column(db.String(255), nullable=True)
    link_token = db.Column(db.String(255), nullable=True)
    link_token_used = db.Column(db.Boolean, default=False, nullable=False)
    linked_at = db.Column(db.BigInteger, nullable=True)

    __table_args__ = (db.CheckConstraint("id = 1", name="singleton_federation_config"),)
```

- [ ] **Step 4: Remove old models and dead columns**

In `backend/app/models.py`:
- Delete the `Node` class (lines 142–182)
- Delete the `NodeClientTraffic` class (lines 185–202)
- Remove `global_limit_bytes` from `Client` (line 73)
- Remove `allowed_node_groups` from `Client` (line 76)
- Remove `master_disabled` from `Inbound` (line 53)
- Remove `allowed_node_groups` from `TariffItem` (line 306)
- Add `panel_id` to `TariffItem`:

```python
panel_id = db.Column(db.Integer, db.ForeignKey("linked_panel.id"), nullable=True)
```

- Remove `allowed_node_groups` from `Client.to_dict()` and `Inbound.to_dict()` (search for these in to_dict methods)
- Remove `master_disabled` from `Inbound.to_dict()`
- Remove `global_limit_bytes` from `Client.to_dict()`

- [ ] **Step 5: Update conftest.py to seed FederationConfig singleton**

The test `app` fixture in `backend/tests/conftest.py` creates all tables via `db.create_all()`. After that call, add:

```python
from app.models import FederationConfig
db.session.add(FederationConfig(id=1))
db.session.commit()
```

- [ ] **Step 6: Run tests — expect PASS**

```bash
cd backend && python -m pytest tests/test_models_federation.py -v
```

- [ ] **Step 7: Fix any existing tests broken by model removal**

Run the full test suite. Tests referencing `Node`, `NodeClientTraffic`, `global_limit_bytes`, `allowed_node_groups`, or `master_disabled` will fail. For each:
- Tests in `test_inbound_master_disabled.py` → delete the file
- Tests in `test_provisioning.py` referencing `allowed_node_groups` → remove that field from test data
- Tests in `test_api_users.py` patching `node_sync` → remove the mock/patch
- Tests in `test_api_bot_tariffs.py` using `allowed_node_groups` → remove field from test payloads

```bash
cd backend && python -m pytest tests/ -v
```

- [ ] **Step 8: Commit**

```bash
git add backend/app/models.py backend/tests/
git commit -m "feat(models): add LinkedPanel + FederationConfig, remove Node system models"
```

---

### Task 2: Migration Script for Existing Installs

**Files:**
- Create: `backend/scripts/migrate_v14_to_v15.py`

- [ ] **Step 1: Write the migration script**

```python
#!/usr/bin/env python3
"""One-time migration from schema v14 (Node system) to v15 (Panel federation).

Run BEFORE upgrading to the new version:
    python scripts/migrate_v14_to_v15.py /path/to/panel.db

Back up your database first!
"""
import sqlite3
import sys


def migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    cur.execute("PRAGMA user_version")
    version = cur.fetchone()[0]
    if version != 14:
        print(f"ERROR: Expected schema version 14, got {version}. Aborting.")
        sys.exit(1)

    print(f"Migrating {db_path} from v14 to v15...")

    # 1. Recreate client table without global_limit_bytes, allowed_node_groups
    print("  Recreating client table...")
    cur.execute("PRAGMA table_info(client)")
    cols = [row[1] for row in cur.fetchall()]
    keep_cols = [c for c in cols if c not in ("global_limit_bytes", "allowed_node_groups")]
    cols_csv = ", ".join(keep_cols)

    cur.execute(f"CREATE TABLE client_new AS SELECT {cols_csv} FROM client")
    cur.execute("DROP TABLE client")
    cur.execute("ALTER TABLE client_new RENAME TO client")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_client_tag_email ON client (inbound_tag, email)")

    # 2. Recreate inbound table without master_disabled
    print("  Recreating inbound table...")
    cur.execute("PRAGMA table_info(inbound)")
    cols = [row[1] for row in cur.fetchall()]
    keep_cols = [c for c in cols if c != "master_disabled"]
    cols_csv = ", ".join(keep_cols)

    cur.execute(f"CREATE TABLE inbound_new AS SELECT {cols_csv} FROM inbound")
    cur.execute("DROP TABLE inbound")
    cur.execute("ALTER TABLE inbound_new RENAME TO inbound")

    # 3. Remove allowed_node_groups from tariff_item
    print("  Recreating tariff_item table...")
    cur.execute("PRAGMA table_info(tariff_item)")
    cols = [row[1] for row in cur.fetchall()]
    keep_cols = [c for c in cols if c != "allowed_node_groups"]
    cols_csv = ", ".join(keep_cols)

    cur.execute(f"CREATE TABLE tariff_item_new AS SELECT {cols_csv} FROM tariff_item")
    cur.execute("DROP TABLE tariff_item")
    cur.execute("ALTER TABLE tariff_item_new RENAME TO tariff_item")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_tariff_item_tariff ON tariff_item (tariff_id)")

    # 4. Drop old tables
    print("  Dropping node tables...")
    cur.execute("DROP TABLE IF EXISTS node_client_traffic")
    cur.execute("DROP TABLE IF EXISTS node")

    # 5. Clean up SystemSetting
    print("  Removing master_groups setting...")
    cur.execute("DELETE FROM system_setting WHERE key = 'master_groups'")

    # 6. Add panel_id to tariff_item
    print("  Adding panel_id to tariff_item...")
    cur.execute("ALTER TABLE tariff_item ADD COLUMN panel_id INTEGER REFERENCES linked_panel(id)")

    # 7. Create new tables
    print("  Creating linked_panel table...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS linked_panel (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL UNIQUE,
            url              TEXT    NOT NULL,
            federation_token TEXT    NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'unknown',
            last_poll        BIGINT,
            last_error       TEXT,
            enable           BOOLEAN NOT NULL DEFAULT 1,
            created_at       BIGINT  NOT NULL
        )
    """)

    print("  Creating federation_config table...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS federation_config (
            id                INTEGER PRIMARY KEY CHECK (id = 1),
            master_url        TEXT,
            master_name       TEXT,
            federation_token  TEXT,
            link_token        TEXT,
            link_token_used   BOOLEAN NOT NULL DEFAULT 0,
            linked_at         BIGINT
        )
    """)
    cur.execute("INSERT OR IGNORE INTO federation_config (id) VALUES (1)")

    # 8. Bump version
    cur.execute("PRAGMA user_version = 15")

    conn.commit()
    conn.close()
    print("Migration complete: v14 → v15")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path-to-panel.db>")
        sys.exit(1)
    migrate(sys.argv[1])
```

- [ ] **Step 2: Commit**

```bash
git add backend/scripts/migrate_v14_to_v15.py
git commit -m "feat: add manual migration script v14→v15 for existing installs"
```

---

### Task 3: Update db_migration.py for New Installs

**Files:**
- Modify: `backend/db_migration.py`

- [ ] **Step 1: Bump CURRENT_DB_VERSION to 15**

Change line 6: `CURRENT_DB_VERSION = 14` → `CURRENT_DB_VERSION = 15`

- [ ] **Step 2: Replace node table functions with federation tables**

Replace `_ensure_node_table` (lines 137–176) and `_ensure_node_client_traffic_table` (lines 179–197) with:

```python
def _ensure_linked_panel_table(cursor: sqlite3.Cursor) -> int:
    if _table_exists(cursor, "linked_panel"):
        return 0
    cursor.execute("""
        CREATE TABLE linked_panel (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL UNIQUE,
            url              TEXT    NOT NULL,
            federation_token TEXT    NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'unknown',
            last_poll        BIGINT,
            last_error       TEXT,
            enable           BOOLEAN NOT NULL DEFAULT 1,
            created_at       BIGINT  NOT NULL
        )
    """)
    return 1


def _ensure_federation_config_table(cursor: sqlite3.Cursor) -> int:
    if _table_exists(cursor, "federation_config"):
        return 0
    cursor.execute("""
        CREATE TABLE federation_config (
            id                INTEGER PRIMARY KEY CHECK (id = 1),
            master_url        TEXT,
            master_name       TEXT,
            federation_token  TEXT,
            link_token        TEXT,
            link_token_used   BOOLEAN NOT NULL DEFAULT 0,
            linked_at         BIGINT
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO federation_config (id) VALUES (1)")
    return 1
```

- [ ] **Step 3: Update the main migration function**

In `migrate_sqlite_db`, replace calls to `_ensure_node_table(cursor)` and `_ensure_node_client_traffic_table(cursor)` with `_ensure_linked_panel_table(cursor)` and `_ensure_federation_config_table(cursor)`.

Also add `panel_id` to the tariff_item schema columns section, and remove `master_disabled` from the inbound schema, `global_limit_bytes`/`allowed_node_groups` from client schema, `allowed_node_groups` from tariff_item schema.

- [ ] **Step 4: Run migration on a test DB**

```bash
cd backend && python -c "
import sqlite3, tempfile, os
path = tempfile.mktemp(suffix='.db')
from db_migration import migrate_sqlite_db
migrate_sqlite_db(path)
conn = sqlite3.connect(path)
cur = conn.cursor()
cur.execute('PRAGMA user_version')
print('version:', cur.fetchone()[0])
cur.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")
print('tables:', [r[0] for r in cur.fetchall()])
assert not any('node' == r[0] for r in cur.execute(\"SELECT name FROM sqlite_master WHERE type='table'\"))
os.unlink(path)
print('OK')
"
```

- [ ] **Step 5: Commit**

```bash
git add backend/db_migration.py
git commit -m "feat(migration): schema v15 — linked_panel + federation_config, drop node tables"
```

---

### Task 4: Federation Auth Decorators

**Files:**
- Modify: `backend/app/utils.py`
- Test: `backend/tests/test_federation_auth.py`

- [ ] **Step 1: Write tests**

```python
# backend/tests/test_federation_auth.py
import pytest
from flask import Flask, jsonify
from app.models import FederationConfig
from app.utils import federation_token_required, admin_or_federation_token_required


@pytest.fixture
def app_with_federation(app, db):
    cfg = FederationConfig.query.get(1)
    cfg.federation_token = "valid-fed-token"
    cfg.master_url = "https://master.example.com"
    db.session.commit()

    @app.route("/test-fed")
    @federation_token_required
    def _fed_endpoint():
        return jsonify({"ok": True})

    @app.route("/test-admin-or-fed")
    @admin_or_federation_token_required
    def _dual_endpoint():
        return jsonify({"ok": True})

    return app


def test_federation_token_valid(app_with_federation):
    client = app_with_federation.test_client()
    resp = client.get("/test-fed", headers={"X-Federation-Token": "valid-fed-token"})
    assert resp.status_code == 200


def test_federation_token_missing(app_with_federation):
    client = app_with_federation.test_client()
    resp = client.get("/test-fed")
    assert resp.status_code == 401


def test_federation_token_wrong(app_with_federation):
    client = app_with_federation.test_client()
    resp = client.get("/test-fed", headers={"X-Federation-Token": "wrong-token"})
    assert resp.status_code == 401


def test_federation_token_not_configured(app, db):
    @app.route("/test-fed-noconfig")
    @federation_token_required
    def _ep():
        return jsonify({"ok": True})

    client = app.test_client()
    resp = client.get("/test-fed-noconfig", headers={"X-Federation-Token": "anything"})
    assert resp.status_code == 401


def test_admin_or_federation_accepts_federation(app_with_federation):
    client = app_with_federation.test_client()
    resp = client.get("/test-admin-or-fed", headers={"X-Federation-Token": "valid-fed-token"})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
cd backend && python -m pytest tests/test_federation_auth.py -v
```

- [ ] **Step 3: Implement decorators in `backend/app/utils.py`**

Add after the existing `admin_or_bot_token_required` decorator (after line ~191):

```python
def _check_federation_token(token: str) -> bool:
    import hmac
    from app.models import FederationConfig

    cfg = FederationConfig.query.get(1)
    if cfg is None or not cfg.federation_token:
        return False
    return hmac.compare_digest(token, cfg.federation_token)


def federation_token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-Federation-Token", "")
        if not token or not _check_federation_token(token):
            return jsonify({"error": "invalid or missing federation token"}), 401
        return f(*args, **kwargs)
    return decorated


def admin_or_federation_token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        fed_token = request.headers.get("X-Federation-Token", "")
        if fed_token and _check_federation_token(fed_token):
            return f(*args, **kwargs)
        # Fall through to admin JWT check (same logic as admin_or_bot_token_required minus bot)
        header = request.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return jsonify({"message": "Token is missing!"}), 401
        token = value.strip()

        if _check_bot_service_token(token):
            return f(*args, **kwargs)

        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"message": "Token is expired!"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"message": "Token is invalid!"}), 401

        if payload.get("role") != "admin":
            return jsonify({"message": "Token is invalid!"}), 401

        from app.models import Admin
        from app.extensions import db

        admin = None
        admin_id = payload.get("admin_id")
        if admin_id is not None:
            try:
                admin = db.session.get(Admin, int(admin_id))
            except (TypeError, ValueError):
                admin = None
        if admin is None and payload.get("user"):
            admin = Admin.query.filter_by(username=payload.get("user")).first()
        if admin is None:
            return jsonify({"message": "Token is invalid!"}), 401

        token_pwd_version = payload.get("pwdv")
        try:
            token_pwd_version = int(token_pwd_version)
        except (TypeError, ValueError):
            return jsonify({"message": "Token is invalid!"}), 401

        current_pwd_version = int(admin.password_changed_at or 0)
        if token_pwd_version != current_pwd_version:
            return jsonify({"message": "Token is invalid!"}), 401
        return f(*args, **kwargs)
    return decorated
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd backend && python -m pytest tests/test_federation_auth.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/utils.py backend/tests/test_federation_auth.py
git commit -m "feat(auth): federation_token_required + admin_or_federation_token_required decorators"
```

---

### Task 5: Federation API Blueprint (Child-Side)

**Files:**
- Create: `backend/app/api/federation.py`
- Test: `backend/tests/test_api_federation.py`

- [ ] **Step 1: Write tests for link-token, handshake, and snapshot**

```python
# backend/tests/test_api_federation.py
import json
import time
import pytest
import jwt
from app.models import FederationConfig, Inbound, Client

SECRET_KEY = "test-secret-key-for-pytest-only"


@pytest.fixture
def app_fed(app, db):
    from app.api import federation
    if not any(bp.name == "federation" for bp in app.blueprints.values()):
        app.register_blueprint(federation.bp, url_prefix="/api")
    return app


@pytest.fixture
def admin_headers(app_fed, db):
    from app.models import Admin
    pwd_v = int(time.time())
    admin = Admin(username="admin", password="x", password_changed_at=pwd_v)
    db.session.add(admin)
    db.session.commit()
    token = jwt.encode(
        {"user": "admin", "admin_id": admin.id, "role": "admin", "pwdv": pwd_v,
         "exp": time.time() + 3600},
        SECRET_KEY, algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_generate_link_token(app_fed, admin_headers):
    c = app_fed.test_client()
    resp = c.post("/api/federation/link-token", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert "link_token" in data
    assert len(data["link_token"]) > 20


def test_generate_link_token_rejects_if_already_linked(app_fed, admin_headers, db):
    cfg = FederationConfig.query.get(1)
    cfg.federation_token = "already-linked"
    cfg.linked_at = int(time.time() * 1000)
    db.session.commit()

    c = app_fed.test_client()
    resp = c.post("/api/federation/link-token", headers=admin_headers)
    assert resp.status_code == 409


def test_handshake_success(app_fed, admin_headers, db):
    c = app_fed.test_client()
    # First generate a link token
    resp = c.post("/api/federation/link-token", headers=admin_headers)
    link_token = resp.get_json()["link_token"]

    # Now handshake
    resp = c.post("/api/federation/handshake", json={
        "link_token": link_token,
        "master_url": "https://master.example.com",
        "master_name": "Master Panel",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert "federation_token" in data
    assert data["name"] is not None

    # Verify config updated
    cfg = FederationConfig.query.get(1)
    assert cfg.link_token_used is True
    assert cfg.master_url == "https://master.example.com"
    assert cfg.federation_token == data["federation_token"]


def test_handshake_rejects_wrong_token(app_fed):
    c = app_fed.test_client()
    resp = c.post("/api/federation/handshake", json={
        "link_token": "wrong-token",
        "master_url": "https://master.example.com",
        "master_name": "Master",
    })
    assert resp.status_code == 401


def test_snapshot_returns_inbounds(app_fed, admin_headers, db):
    c = app_fed.test_client()
    # Link first
    resp = c.post("/api/federation/link-token", headers=admin_headers)
    link_token = resp.get_json()["link_token"]
    resp = c.post("/api/federation/handshake", json={
        "link_token": link_token,
        "master_url": "https://m.com",
        "master_name": "M",
    })
    fed_token = resp.get_json()["federation_token"]

    # Create an inbound with a client
    ib = Inbound(tag="vless-tcp", port=443, protocol="vless", stream_settings="{}")
    db.session.add(ib)
    db.session.flush()
    cl = Client(
        id="test-uuid", email="user1", inbound_tag="vless-tcp",
        limit_bytes=0, expiry_time=0, up=100, down=200, enable=True,
    )
    db.session.add(cl)
    db.session.commit()

    resp = c.get("/api/federation/snapshot", headers={"X-Federation-Token": fed_token})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert len(data["inbounds"]) == 1
    assert data["inbounds"][0]["tag"] == "vless-tcp"
    assert len(data["inbounds"][0]["clients"]) == 1
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
cd backend && python -m pytest tests/test_api_federation.py -v
```

- [ ] **Step 3: Implement federation blueprint**

```python
# backend/app/api/federation.py
import secrets
import time

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.models import FederationConfig, Inbound, Client
from app.utils import token_required, federation_token_required

bp = Blueprint("federation", __name__)


@bp.route("/federation/link-token", methods=["POST"])
@token_required
def generate_link_token():
    cfg = FederationConfig.query.get(1)
    if cfg.federation_token and cfg.linked_at:
        return jsonify({"error": "Panel is already linked to a master"}), 409

    cfg.link_token = secrets.token_urlsafe(32)
    cfg.link_token_used = False
    db.session.commit()
    return jsonify({"link_token": cfg.link_token})


@bp.route("/federation/handshake", methods=["POST"])
def handshake():
    data = request.get_json(silent=True) or {}
    link_token = data.get("link_token", "")
    master_url = data.get("master_url", "")
    master_name = data.get("master_name", "")

    if not link_token or not master_url:
        return jsonify({"error": "link_token and master_url required"}), 400

    cfg = FederationConfig.query.get(1)
    if not cfg.link_token or cfg.link_token_used:
        return jsonify({"error": "no valid link token — generate a new one"}), 401

    import hmac
    if not hmac.compare_digest(link_token, cfg.link_token):
        return jsonify({"error": "invalid link token"}), 401

    federation_token = secrets.token_urlsafe(32)
    cfg.federation_token = federation_token
    cfg.master_url = master_url
    cfg.master_name = master_name
    cfg.link_token_used = True
    cfg.linked_at = int(time.time() * 1000)
    db.session.commit()

    from app.models import SystemSetting
    panel_name_row = SystemSetting.query.filter_by(key="panel_name").first()
    panel_name = panel_name_row.value if panel_name_row and panel_name_row.value else "Panel"

    return jsonify({
        "federation_token": federation_token,
        "name": panel_name,
        "panel_version": 15,
        "inbound_count": Inbound.query.count(),
    })


@bp.route("/federation/snapshot", methods=["GET"])
@federation_token_required
def snapshot():
    from app.models import SystemSetting
    panel_name_row = SystemSetting.query.filter_by(key="panel_name").first()
    panel_name = panel_name_row.value if panel_name_row and panel_name_row.value else "Panel"

    inbounds = Inbound.query.all()
    result = []
    for ib in inbounds:
        clients = Client.query.filter_by(inbound_tag=ib.tag).all()
        result.append({
            "tag": ib.tag,
            "port": ib.port,
            "protocol": ib.protocol,
            "label": ib.label,
            "stream_settings": ib.stream_settings if isinstance(ib.stream_settings, dict)
                               else _safe_json_parse(ib.stream_settings),
            "up": ib.up or 0,
            "down": ib.down or 0,
            "fallback_address": ib.fallback_address,
            "device_limit": ib.device_limit or 0,
            "routing_profile_id": ib.routing_profile_id,
            "clients": [
                {
                    "id": c.id,
                    "email": c.email,
                    "enable": bool(c.enable),
                    "up": c.up or 0,
                    "down": c.down or 0,
                    "limit_bytes": c.limit_bytes or 0,
                    "expiry_time": c.expiry_time or 0,
                    "reset_day": c.reset_day or 0,
                    "flow": c.flow or "",
                    "last_seen": c.last_seen,
                    "device_count": getattr(c, "_device_count", 0),
                    "tariff_id": c.tariff_id,
                    "telegram_id": c.telegram_id,
                }
                for c in clients
            ],
        })

    return jsonify({
        "panel_name": panel_name,
        "status": "ok",
        "timestamp": int(time.time() * 1000),
        "inbounds": result,
    })


@bp.route("/federation/provision", methods=["POST"])
@limiter.exempt
@federation_token_required
def provision():
    """NOTE: provision_single_item is added to provisioning.py in Task 9 Step 5.
    This endpoint will raise ImportError until then — that's expected during incremental development."""
    data = request.get_json(silent=True) or {}
    telegram_id = data.get("telegram_id")
    inbound_tag = data.get("inbound_tag")
    expiry_ms = data.get("expiry_ms", 0)
    limit_bytes = data.get("limit_bytes", 0)
    tariff_id = data.get("tariff_id")

    if not telegram_id or not inbound_tag:
        return jsonify({"error": "telegram_id and inbound_tag required"}), 400

    try:
        from app.services.provisioning import provision_single_item
        result = provision_single_item(
            telegram_id=int(telegram_id),
            inbound_tag=inbound_tag,
            expiry_ms=int(expiry_ms),
            limit_bytes=int(limit_bytes),
            tariff_id=tariff_id,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "provisioning failed"}), 500


def _safe_json_parse(val):
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    try:
        import json
        return json.loads(val)
    except Exception:
        return {}
```

- [ ] **Step 4: Exempt federation endpoints from rate limiting**

In `backend/app/api/federation.py`, add at the top of each endpoint that the polling will hit:

```python
from app.extensions import limiter

# Add this decorator BEFORE @federation_token_required on the snapshot endpoint:
@bp.route("/federation/snapshot", methods=["GET"])
@limiter.exempt
@federation_token_required
def snapshot():
    ...
```

Also exempt the handshake endpoint:
```python
@bp.route("/federation/handshake", methods=["POST"])
@limiter.exempt
def handshake():
    ...
```

- [ ] **Step 5: Run tests — expect PASS**

```bash
cd backend && python -m pytest tests/test_api_federation.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/federation.py backend/tests/test_api_federation.py
git commit -m "feat(api): federation blueprint — link-token, handshake, snapshot, provision"
```

---

### Task 6: Panels API Blueprint (Master-Side)

**Files:**
- Create: `backend/app/api/panels.py`
- Test: `backend/tests/test_api_panels.py`

- [ ] **Step 1: Write tests for panel CRUD**

```python
# backend/tests/test_api_panels.py
import time
import pytest
import jwt
from unittest.mock import patch, MagicMock
from app.models import LinkedPanel

SECRET_KEY = "test-secret-key-for-pytest-only"


@pytest.fixture
def app_panels(app, db):
    from app.api import panels
    if not any(bp.name == "panels" for bp in app.blueprints.values()):
        app.register_blueprint(panels.bp, url_prefix="/api")
    return app


@pytest.fixture
def admin_headers(app_panels, db):
    from app.models import Admin
    pwd_v = int(time.time())
    admin = Admin(username="admin", password="x", password_changed_at=pwd_v)
    db.session.add(admin)
    db.session.commit()
    token = jwt.encode(
        {"user": "admin", "admin_id": admin.id, "role": "admin", "pwdv": pwd_v,
         "exp": time.time() + 3600},
        SECRET_KEY, algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_list_panels_empty(app_panels, admin_headers):
    c = app_panels.test_client()
    resp = c.get("/api/panels", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_create_panel_calls_handshake(app_panels, admin_headers, db):
    handshake_response = MagicMock()
    handshake_response.status_code = 200
    handshake_response.json.return_value = {
        "federation_token": "fed-token-from-child",
        "name": "DE-1",
        "panel_version": 15,
        "inbound_count": 3,
    }

    with patch("app.api.panels.requests.post", return_value=handshake_response):
        c = app_panels.test_client()
        resp = c.post("/api/panels", json={
            "name": "DE-1",
            "url": "https://de1.example.com/secret",
            "link_token": "child-link-token",
        }, headers=admin_headers)

    assert resp.status_code == 201
    data = resp.get_json()
    assert data["name"] == "DE-1"

    panel = LinkedPanel.query.first()
    assert panel.federation_token == "fed-token-from-child"


def test_delete_panel(app_panels, admin_headers, db):
    panel = LinkedPanel(
        name="US-1", url="https://us.com", federation_token="t",
        created_at=int(time.time() * 1000),
    )
    db.session.add(panel)
    db.session.commit()

    c = app_panels.test_client()
    resp = c.delete(f"/api/panels/{panel.id}", headers=admin_headers)
    assert resp.status_code == 200
    assert LinkedPanel.query.count() == 0
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
cd backend && python -m pytest tests/test_api_panels.py -v
```

- [ ] **Step 3: Implement panels blueprint**

```python
# backend/app/api/panels.py
import logging
import time

import requests
from flask import Blueprint, jsonify, request

from app.extensions import db
from app.models import LinkedPanel
from app.utils import token_required

bp = Blueprint("panels", __name__)
logger = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 10


@bp.route("/panels", methods=["GET"])
@token_required
def list_panels():
    panels = LinkedPanel.query.order_by(LinkedPanel.id).all()
    return jsonify([p.to_dict() for p in panels])


@bp.route("/panels", methods=["POST"])
@token_required
def create_panel():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip().rstrip("/")
    link_token = (data.get("link_token") or "").strip()

    if not name or not url or not link_token:
        return jsonify({"error": "name, url, and link_token are required"}), 400
    if LinkedPanel.query.filter_by(name=name).first():
        return jsonify({"error": f"Panel '{name}' already exists"}), 409

    from app.models import SystemSetting
    master_name_row = SystemSetting.query.filter_by(key="panel_name").first()
    master_name = master_name_row.value if master_name_row and master_name_row.value else "Master"

    try:
        resp = requests.post(
            f"{url}/api/federation/handshake",
            json={
                "link_token": link_token,
                "master_url": request.host_url.rstrip("/"),
                "master_name": master_name,
            },
            timeout=HANDSHAKE_TIMEOUT,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"Cannot reach panel: {e}"}), 502

    if resp.status_code != 200:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        return jsonify({"error": body.get("error", f"Handshake failed ({resp.status_code})")}), 502

    result = resp.json()
    panel = LinkedPanel(
        name=name,
        url=url,
        federation_token=result["federation_token"],
        status="unknown",
        enable=True,
        created_at=int(time.time() * 1000),
    )
    db.session.add(panel)
    db.session.commit()

    return jsonify(panel.to_dict()), 201


@bp.route("/panels/<int:panel_id>", methods=["PUT"])
@token_required
def update_panel(panel_id):
    panel = db.session.get(LinkedPanel, panel_id)
    if not panel:
        return jsonify({"error": "Panel not found"}), 404

    data = request.get_json(silent=True) or {}
    if "name" in data:
        new_name = (data["name"] or "").strip()
        if new_name and new_name != panel.name:
            if LinkedPanel.query.filter_by(name=new_name).first():
                return jsonify({"error": f"Name '{new_name}' already taken"}), 409
            panel.name = new_name
    if "enable" in data:
        panel.enable = bool(data["enable"])

    db.session.commit()
    return jsonify(panel.to_dict())


@bp.route("/panels/<int:panel_id>", methods=["DELETE"])
@token_required
def delete_panel(panel_id):
    panel = db.session.get(LinkedPanel, panel_id)
    if not panel:
        return jsonify({"error": "Panel not found"}), 404

    from app.extensions import get_redis
    r = get_redis()
    if r:
        try:
            r.delete(f"panel:{panel.id}:snapshot")
            r.delete(f"panel:{panel.id}:status")
        except Exception:
            pass

    db.session.delete(panel)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/panels/<int:panel_id>/test", methods=["POST"])
@token_required
def test_panel(panel_id):
    panel = db.session.get(LinkedPanel, panel_id)
    if not panel:
        return jsonify({"error": "Panel not found"}), 404

    start = time.time()
    try:
        resp = requests.get(
            f"{panel.url}/api/federation/snapshot",
            headers={"X-Federation-Token": panel.federation_token},
            timeout=5,
        )
        latency_ms = int((time.time() - start) * 1000)
        if resp.status_code == 200:
            panel.status = "online"
            panel.last_poll = int(time.time() * 1000)
            panel.last_error = None
        else:
            panel.status = "offline"
            panel.last_error = f"HTTP {resp.status_code}"
    except requests.RequestException as e:
        latency_ms = int((time.time() - start) * 1000)
        panel.status = "offline"
        panel.last_error = str(e)

    db.session.commit()
    return jsonify({**panel.to_dict(), "latency_ms": latency_ms})
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd backend && python -m pytest tests/test_api_panels.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/panels.py backend/tests/test_api_panels.py
git commit -m "feat(api): panels blueprint — CRUD + handshake + test connection"
```

---

### Task 7: FederationClient + Proxy Layer

**Files:**
- Create: `backend/app/services/panel_proxy.py`
- Test: `backend/tests/test_panel_proxy.py`

- [ ] **Step 1: Write tests**

```python
# backend/tests/test_panel_proxy.py
import json
import pytest
from unittest.mock import patch, MagicMock
from app.models import LinkedPanel
from app.services.panel_proxy import FederationClient, proxy_create_user


@pytest.fixture
def panel(app, db):
    p = LinkedPanel(
        name="DE-1", url="https://de1.example.com/secret",
        federation_token="fed-token", status="online",
        created_at=1000,
    )
    db.session.add(p)
    db.session.commit()
    return p


def test_federation_client_snapshot(panel):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok", "inbounds": []}

    with patch("app.services.panel_proxy.requests.Session") as MockSession:
        MockSession.return_value.get.return_value = mock_resp
        client = FederationClient(panel.url, panel.federation_token)
        result = client.snapshot()
        assert result["status"] == "ok"


def test_proxy_create_user_offline(app, db, panel):
    panel.status = "offline"
    db.session.commit()
    with pytest.raises(ValueError, match="offline"):
        proxy_create_user(panel.id, "vless-tcp", {"email": "test"})
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
cd backend && python -m pytest tests/test_panel_proxy.py -v
```

- [ ] **Step 3: Implement FederationClient and proxy functions**

```python
# backend/app/services/panel_proxy.py
import json
import logging

import requests

from app.extensions import db, get_redis
from app.models import LinkedPanel

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 8


class FederationClient:
    def __init__(self, url: str, federation_token: str):
        self.base_url = url.rstrip("/")
        self.token = federation_token
        self._session = requests.Session()
        self._session.headers["X-Federation-Token"] = self.token

    def snapshot(self) -> dict:
        resp = self._session.get(f"{self.base_url}/api/federation/snapshot", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def create_inbound(self, payload: dict) -> dict:
        resp = self._session.post(f"{self.base_url}/api/inbounds", json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def update_inbound(self, tag: str, payload: dict) -> dict:
        resp = self._session.put(f"{self.base_url}/api/inbounds/{tag}", json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def delete_inbound(self, tag: str) -> dict:
        resp = self._session.delete(f"{self.base_url}/api/inbounds/{tag}", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def create_user(self, tag: str, user_data: dict) -> dict:
        resp = self._session.post(f"{self.base_url}/api/inbounds/{tag}/users", json=user_data, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def update_user(self, tag: str, user_data: dict) -> dict:
        resp = self._session.put(f"{self.base_url}/api/inbounds/{tag}/users", json=user_data, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def delete_user(self, tag: str, email: str) -> dict:
        resp = self._session.delete(
            f"{self.base_url}/api/inbounds/{tag}/users",
            params={"email": email},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def provision(self, telegram_id: int, inbound_tag: str, params: dict) -> dict:
        resp = self._session.post(
            f"{self.base_url}/api/federation/provision",
            json={"telegram_id": telegram_id, "inbound_tag": inbound_tag, **params},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


def _get_panel_or_raise(panel_id: int) -> LinkedPanel:
    panel = db.session.get(LinkedPanel, panel_id)
    if panel is None:
        raise ValueError(f"Panel {panel_id} not found")
    if panel.status == "offline":
        raise ValueError(f"Panel '{panel.name}' is offline")
    return panel


def _refresh_panel_cache(panel: LinkedPanel) -> None:
    try:
        client = FederationClient(panel.url, panel.federation_token)
        data = client.snapshot()
        r = get_redis()
        if r:
            r.setex(f"panel:{panel.id}:snapshot", 60, json.dumps(data))
            r.setex(f"panel:{panel.id}:status", 120, "online")
        panel.status = "online"
        panel.last_poll = data.get("timestamp")
        panel.last_error = None
        db.session.commit()
    except Exception as exc:
        logger.warning("Failed to refresh cache for panel %s: %s", panel.name, exc)


def proxy_create_user(panel_id: int, inbound_tag: str, user_data: dict) -> dict:
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.create_user(inbound_tag, user_data)
    _refresh_panel_cache(panel)
    return result


def proxy_update_user(panel_id: int, inbound_tag: str, user_data: dict) -> dict:
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.update_user(inbound_tag, user_data)
    _refresh_panel_cache(panel)
    return result


def proxy_delete_user(panel_id: int, inbound_tag: str, email: str) -> dict:
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.delete_user(inbound_tag, email)
    _refresh_panel_cache(panel)
    return result


def proxy_create_inbound(panel_id: int, payload: dict) -> dict:
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.create_inbound(payload)
    _refresh_panel_cache(panel)
    return result


def proxy_update_inbound(panel_id: int, tag: str, payload: dict) -> dict:
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.update_inbound(tag, payload)
    _refresh_panel_cache(panel)
    return result


def proxy_delete_inbound(panel_id: int, tag: str) -> dict:
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.delete_inbound(tag)
    _refresh_panel_cache(panel)
    return result


def proxy_provision(panel_id: int, telegram_id: int, inbound_tag: str, params: dict) -> dict:
    panel = _get_panel_or_raise(panel_id)
    client = FederationClient(panel.url, panel.federation_token)
    result = client.provision(telegram_id, inbound_tag, params)
    _refresh_panel_cache(panel)
    return result


def get_panel_snapshot(panel_id: int) -> dict | None:
    r = get_redis()
    if not r:
        return None
    raw = r.get(f"panel:{panel_id}:snapshot")
    if raw is None:
        return None
    return json.loads(raw)
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd backend && python -m pytest tests/test_panel_proxy.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/panel_proxy.py backend/tests/test_panel_proxy.py
git commit -m "feat: FederationClient HTTP client + panel_proxy service"
```

---

### Task 8: Polling Scheduler Job

**Files:**
- Create: `backend/app/jobs/panels.py`
- Modify: `backend/app/__init__.py`

- [ ] **Step 1: Implement poll_linked_panels job**

```python
# backend/app/jobs/panels.py
import json
import logging
import time

import gevent.pool

from app.extensions import db, get_redis
from app.models import LinkedPanel
from app.services.panel_proxy import FederationClient

logger = logging.getLogger(__name__)


def poll_linked_panels():
    panels = LinkedPanel.query.filter_by(enable=True).all()
    if not panels:
        return

    pool = gevent.pool.Pool(size=10)

    def _poll_one(panel_id: int, url: str, token: str, name: str):
        try:
            client = FederationClient(url, token)
            data = client.snapshot()
            r = get_redis()
            if r:
                r.setex(f"panel:{panel_id}:snapshot", 60, json.dumps(data))
                r.setex(f"panel:{panel_id}:status", 120, "online")
            return panel_id, "online", None, data.get("timestamp")
        except Exception as exc:
            return panel_id, "offline", str(exc)[:500], None

    jobs = []
    for p in panels:
        jobs.append(pool.spawn(_poll_one, p.id, p.url, p.federation_token, p.name))

    pool.join()

    for job in jobs:
        panel_id, status, error, ts = job.value
        panel = db.session.get(LinkedPanel, panel_id)
        if panel is None:
            continue
        panel.status = status
        if status == "online":
            panel.last_poll = ts or int(time.time() * 1000)
            panel.last_error = None
        else:
            panel.last_error = error

    db.session.commit()
```

- [ ] **Step 2: Update `backend/app/__init__.py`**

Replace the node-related imports (lines 20–25):
```python
# REMOVE these:
from .services.node_sync import (
    node_health_check_job,
    node_user_sync_job,
    node_inbound_sync_job,
    node_traffic_poll_job,
)
# ADD this:
from .jobs.panels import poll_linked_panels
```

Replace the 4 node scheduler jobs (lines ~178–181):
```python
# REMOVE these 4 lines:
_ensure_scheduler_job("node_health_check", node_health_check_job, 60)
_ensure_scheduler_job("node_user_sync", node_user_sync_job, 3600)
_ensure_scheduler_job("node_inbound_sync", node_inbound_sync_job, 300)
_ensure_scheduler_job("node_traffic_poll", node_traffic_poll_job, 60)
# ADD this:
_ensure_scheduler_job("poll_linked_panels", poll_linked_panels, 10)
```

Replace the nodes blueprint registration:
```python
# REMOVE:
from .api import ... nodes ...
app.register_blueprint(nodes.bp, url_prefix="/api")
# ADD:
from .api import panels, federation
app.register_blueprint(panels.bp, url_prefix="/api")
app.register_blueprint(federation.bp, url_prefix="/api")
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/jobs/panels.py backend/app/__init__.py
git commit -m "feat: poll_linked_panels scheduler job (10s) + register new blueprints"
```

---

### Task 9: Remove Old Node System + Update Inbound/Subscription/Stats

**Files:**
- Delete: `backend/app/services/node_sync.py`
- Delete: `backend/app/api/nodes.py`
- Modify: `backend/app/api/inbound.py`
- Modify: `backend/app/api/subscription.py`
- Modify: `backend/app/services/stats.py`
- Modify: `backend/app/services/provisioning.py`

- [ ] **Step 1: Delete old files**

```bash
rm backend/app/services/node_sync.py backend/app/api/nodes.py
```

- [ ] **Step 2: Clean up `backend/app/api/inbound.py`**

Remove all `node_sync` imports and calls. Search for these patterns and remove them:

- Any `from app.services.node_sync import ...` lines
- Line ~219: `sync_inbound_to_all_nodes(new_ib)` block (try/except around it)
- Line ~396: `sync_inbound_to_all_nodes(ib)` block
- Line ~432: `sync_inbound_delete_to_all_nodes(deleted_tag)` block
- Line ~508–521: `sync_user_create(...)` block
- Line ~616–630: `sync_user_update(...)` block
- Line ~684–686: `sync_user_delete(...)` call
- Line ~740–743: `sync_user_delete(...)` in bulk delete

Also remove the `_normalize_node_groups` function and any `allowed_node_groups` handling in user create/update.

Add `panel_id` routing at the top of create/update/delete inbound and user endpoints:

```python
# At the top of each inbound/user endpoint that now needs panel routing:
panel_id = request.args.get("panel_id", type=int) or (data.get("panel_id") if data else None)
if panel_id:
    from app.services.panel_proxy import proxy_create_user  # or whichever proxy fn
    try:
        return jsonify(proxy_create_user(panel_id, tag, data))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "Remote panel error"}), 502
# ... rest of existing local logic unchanged
```

Change auth decorators on inbound/user CRUD endpoints from `@admin_or_bot_token_required` to `@admin_or_federation_token_required` (import it from `app.utils`).

- [ ] **Step 3: Clean up `backend/app/api/subscription.py`**

- Remove `from app.models import ... NodeClientTraffic ...`
- Remove `_master_visible_to_client()` function (lines 22–41)
- Remove `_master_groups()` helper
- Remove all `NodeClientTraffic` queries in `_user_headers()` (lines 168–175) — traffic is now local only:

```python
# Replace aggregated traffic with local-only:
upload = int(client.up or 0)
download = int(client.down or 0)
total = int(client.limit_bytes or 0)
```

- Remove `_get_remote_links()`, `_get_remote_clash_proxies()`, `_get_remote_singbox_outbounds()` functions
- Remove calls to these functions from the subscription render endpoints
- Remove `master_disabled` checks (the inbound always runs locally now)

- [ ] **Step 4: Clean up `backend/app/services/stats.py`**

- Remove `from app.models import NodeClientTraffic`
- Remove `_global_node_usage_map()` function
- In `check_limits_and_reset()`, remove `global_limit_bytes` logic and `node_usage` summing. Simplify the over-limit check to local-only:

```python
# Replace:
# global_used = (c.up + c.down) + int(node_usage.get(c.email, 0))
# global_over = (c.global_limit_bytes or 0) > 0 and global_used >= (c.global_limit_bytes or 0)
# per_node_over = c.limit_bytes > 0 and (c.up + c.down) >= c.limit_bytes
# With:
over_limit = c.limit_bytes > 0 and (c.up + c.down) >= c.limit_bytes
```

- [ ] **Step 5: Clean up `backend/app/services/provisioning.py`**

- Remove `from app.services.node_sync import sync_user_create, sync_user_update` (line 14)
- In `_sync_after_provision()`: remove the `sync_user_create`/`sync_user_update` loops (lines 48–88). Keep only `generate_config_file()` and `restart_xray_container()` and `sub_cache.invalidate_user()` calls.
- In `_create_client_for_item()`: remove `allowed_node_groups=item.allowed_node_groups or ""` (line 141)
- In `apply_tariff_for_user()`: remove `client.allowed_node_groups = item.allowed_node_groups or ""` (line 177)
- Add `provision_single_item()` function (called by federation provision endpoint):

```python
def provision_single_item(
    *,
    telegram_id: int,
    inbound_tag: str,
    expiry_ms: int,
    limit_bytes: int,
    tariff_id: int | None = None,
) -> dict:
    now_ms = int(time.time() * 1000)
    inbound = Inbound.query.filter_by(tag=inbound_tag).first()
    if inbound is None:
        raise ValueError(f"Inbound {inbound_tag!r} not found")

    client = Client.query.filter_by(
        telegram_id=telegram_id, inbound_tag=inbound_tag,
    ).first()

    if client is not None:
        client.expiry_time = expiry_ms
        client.limit_bytes = limit_bytes
        client.up = 0
        client.down = 0
        client.last_reset_time = now_ms
        client.enable = True
        if tariff_id is not None:
            client.tariff_id = tariff_id
        NotificationLog.query.filter(
            NotificationLog.client_id == client.id,
            NotificationLog.kind.in_(("traffic_80", "traffic_95", "traffic_exhausted")),
        ).delete(synchronize_session=False)
    else:
        identity = _generate_identity(inbound.protocol)
        base_email = _generate_email(telegram_id, inbound_tag)
        email = base_email
        for _attempt in range(8):
            if not Client.query.filter_by(inbound_tag=inbound_tag, email=email).first():
                break
            email = f"{base_email}_{secrets.token_hex(3)}"
        else:
            raise RuntimeError(f"Could not find unique email for tg={telegram_id}")

        client = Client(
            id=identity, email=email, inbound_tag=inbound_tag,
            telegram_id=telegram_id, tariff_id=tariff_id,
            limit_bytes=limit_bytes, expiry_time=expiry_ms,
            up=0, down=0, enable=True,
            flow="xtls-rprx-vision" if inbound.protocol == "vless" else "",
        )
        db.session.add(client)

    db.session.commit()
    generate_config_file()
    restart_xray_container()
    try:
        sub_cache.invalidate_user(client.id)
    except Exception:
        pass

    return {"client": client.to_dict(), "expires_at_ms": expiry_ms}
```

- [ ] **Step 6: Add panel_id routing to apply_tariff_for_user**

In the `for item in tariff.items:` loop of `apply_tariff_for_user()`, add panel routing:

```python
for item in tariff.items:
    limit_bytes = item.traffic_gb * _GB if item.traffic_gb else 0
    if item.panel_id is not None:
        # Remote panel — proxy the provision
        from app.services.panel_proxy import proxy_provision
        try:
            proxy_provision(
                item.panel_id, telegram_id, item.inbound_tag,
                {"expiry_ms": new_expiry_ms, "limit_bytes": limit_bytes, "tariff_id": tariff.id},
            )
        except Exception as exc:
            logger.warning("proxy_provision failed for panel=%s tag=%s: %s", item.panel_id, item.inbound_tag, exc)
            raise
        continue
    # ... existing local logic below (unchanged)
```

- [ ] **Step 7: Extend GET /api/inbounds to include child panel data**

In `backend/app/api/inbound.py`, modify the `list_inbounds()` endpoint to merge child panel data:

```python
@bp.route("/inbounds", methods=["GET"])
@admin_or_federation_token_required
def list_inbounds():
    panel_filter = request.args.get("panel", "all")

    result = []
    if panel_filter in ("all", "local"):
        # Existing local inbound logic
        inbounds = Inbound.query.all()
        for ib in inbounds:
            d = ib.to_dict()
            d["panel_id"] = None
            d["panel_name"] = "Master"
            result.append(d)

    if panel_filter != "local":
        from app.models import LinkedPanel
        from app.services.panel_proxy import get_panel_snapshot

        if panel_filter == "all":
            panels = LinkedPanel.query.filter_by(enable=True).all()
        else:
            try:
                panels = [LinkedPanel.query.get(int(panel_filter))]
                panels = [p for p in panels if p]
            except (ValueError, TypeError):
                panels = []

        for panel in panels:
            snapshot = get_panel_snapshot(panel.id)
            if snapshot is None:
                continue
            for ib_data in snapshot.get("inbounds", []):
                ib_data["panel_id"] = panel.id
                ib_data["panel_name"] = panel.name
                result.append(ib_data)

    return jsonify(result)
```

- [ ] **Step 8: Run full test suite**

```bash
cd backend && python -m pytest tests/ -v
```

Fix any remaining failures from removed imports/fields.

- [ ] **Step 9: Commit**

```bash
git add -A backend/
git commit -m "feat: remove Node system, add panel_id routing, simplify subscription/stats"
```

---

## Phase 2: Frontend

### Task 10: TypeScript Types + Sidebar + Routes

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/components/layout/Sidebar.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Update types.ts**

Remove `Node` interface (lines 131–146). Add:

```typescript
export interface LinkedPanel {
  id: number;
  name: string;
  url: string;
  federation_token: string;
  status: 'online' | 'offline' | 'unknown';
  last_poll: number | null;
  last_error: string | null;
  enable: boolean;
  created_at: number;
}

export interface FederationConfig {
  master_url: string | null;
  master_name: string | null;
  link_token: string | null;
  link_token_used: boolean;
  linked_at: number | null;
}
```

Extend `Inbound` (add after existing fields, before closing brace at line ~99):
```typescript
  panel_id?: number | null;
  panel_name?: string;
```

Remove from `Inbound`: `master_disabled?: boolean;` (line 98)

Extend `TariffItem` (line ~174): add `panel_id?: number | null;`, remove `allowed_node_groups`

Remove from `Client` (lines 13–32): `global_limit_bytes`, `allowed_node_groups`

- [ ] **Step 2: Rename Nodes → Panels in Sidebar**

In `frontend/src/components/layout/Sidebar.tsx` line 24, change:
```typescript
// FROM:
{ icon: Server, label: 'Nodes', path: '/nodes' },
// TO:
{ icon: Server, label: 'Panels', path: '/panels' },
```

- [ ] **Step 3: Update route in App.tsx**

In `frontend/src/App.tsx` line 50:
```typescript
// FROM:
<Route path="nodes" element={<Nodes />} />
// TO:
<Route path="panels" element={<Panels />} />
```

Update the import at the top:
```typescript
// FROM:
import Nodes from './pages/Nodes';
// TO:
import Panels from './pages/Panels';
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/components/layout/Sidebar.tsx frontend/src/App.tsx
git commit -m "feat(frontend): update types, rename Nodes→Panels route"
```

---

### Task 11: Panels Page

**Files:**
- Create: `frontend/src/pages/Panels.tsx`
- Delete: `frontend/src/pages/Nodes.tsx`

- [ ] **Step 1: Create Panels.tsx**

This is a full rewrite of Nodes.tsx. The page shows:
1. Master panel card (always on top)
2. Child panel cards with status, inbound/user counts, test/edit/unlink buttons
3. "Add Panel" button with modal (name, URL, link token)
4. On child panels: "Linked to: {master}" section + "Generate Link Token" button

Build the component following the existing Nodes.tsx structure (queries, mutations, cards, modals) but adapted for the `LinkedPanel` type and `/api/panels` endpoints. Also add a section that fetches `GET /api/federation/config` (new endpoint returning the `federation_config` singleton) to show the link status when the panel is a child.

Key queries:
- `useQuery<LinkedPanel[]>(['panels'], () => api.get('/panels'))` — refetchInterval 10s
- `useQuery<FederationConfig>(['federation-config'], () => api.get('/federation/config'))` — refetchInterval 30s

Key mutations:
- `POST /panels` — add panel (handshake)
- `PUT /panels/:id` — edit name/enable
- `DELETE /panels/:id` — unlink
- `POST /panels/:id/test` — test connection
- `POST /federation/link-token` — generate link token

Add a `GET /api/federation/config` endpoint in `federation.py`:
```python
@bp.route("/federation/config", methods=["GET"])
@token_required
def get_federation_config():
    cfg = FederationConfig.query.get(1)
    return jsonify({
        "master_url": cfg.master_url,
        "master_name": cfg.master_name,
        "linked_at": cfg.linked_at,
        "link_token": cfg.link_token if not cfg.link_token_used else None,
        "is_linked": bool(cfg.federation_token and cfg.linked_at),
    })
```

- [ ] **Step 2: Delete Nodes.tsx**

```bash
rm frontend/src/pages/Nodes.tsx
```

- [ ] **Step 3: Run typecheck and lint**

```bash
cd frontend && npx tsc --noEmit && npm run lint
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/Panels.tsx backend/app/api/federation.py
git rm frontend/src/pages/Nodes.tsx
git commit -m "feat(frontend): Panels page replaces Nodes — panel cards, add/unlink, link token"
```

---

### Task 12: Dashboard — Panel Filter + Badge

**Files:**
- Modify: `frontend/src/pages/Dashboard.tsx`

- [ ] **Step 1: Add panels query**

Near existing queries (line ~256), add:

```typescript
const { data: panels } = useQuery<LinkedPanel[]>({
  queryKey: ['panels'],
  queryFn: async () => (await api.get<LinkedPanel[]>('/panels')).data,
  refetchOnWindowFocus: false,
  refetchInterval: 10000,
});
```

- [ ] **Step 2: Add panel filter state**

Near `searchTerm` and `statusFilter` states (line ~223), add:

```typescript
const [panelFilter, setPanelFilter] = useState<string>('all');
```

- [ ] **Step 3: Add panel filter dropdown in the filter bar**

In the filter/search section (around line 393–419), add a Select dropdown on the right side:

```tsx
<Select
  value={panelFilter}
  onChange={(e) => setPanelFilter(e.target.value)}
  options={[
    { value: 'all', label: 'All panels' },
    { value: 'local', label: 'Master' },
    ...(panels || []).map(p => ({ value: String(p.id), label: p.name })),
  ]}
/>
```

- [ ] **Step 4: Filter inbounds by panel**

In the `filteredInbounds` memo (lines 291–321), add panel filtering:

```typescript
let filtered = inbounds || [];
if (panelFilter !== 'all') {
  if (panelFilter === 'local') {
    filtered = filtered.filter(ib => !ib.panel_id);
  } else {
    filtered = filtered.filter(ib => String(ib.panel_id) === panelFilter);
  }
}
// ... then existing search + status filtering
```

- [ ] **Step 5: Add panel badge on InboundCard**

In the InboundCard header section (around line ~1119), add a small badge showing the panel name when `panel_id` is set:

```tsx
{inbound.panel_id && inbound.panel_name && (
  <span className="text-[10px] px-1.5 py-0.5 rounded-md bg-white/[0.06] text-white/40 border border-white/[0.05]">
    {inbound.panel_name}
  </span>
)}
```

- [ ] **Step 6: Pass panel_id in create/update/delete mutations**

Update mutations in InboundCard and UserRow to include `panel_id` when the inbound belongs to a child panel. For example, in the add user mutation:

```typescript
mutationFn: (data: any) =>
  inbound.panel_id
    ? api.post(`/inbounds/${inbound.tag}/users?panel_id=${inbound.panel_id}`, data)
    : api.post(`/inbounds/${inbound.tag}/users`, data),
```

- [ ] **Step 7: Disable editing when panel is offline**

In InboundCard, check if the inbound's panel is offline:

```typescript
const panelOffline = inbound.panel_id
  ? panels?.find(p => p.id === inbound.panel_id)?.status === 'offline'
  : false;
```

Use `panelOffline` to disable add/edit/delete buttons with a tooltip.

- [ ] **Step 8: Run typecheck and dev server, test in browser**

```bash
cd frontend && npx tsc --noEmit && npm run dev
```

- [ ] **Step 9: Commit**

```bash
git add frontend/src/pages/Dashboard.tsx
git commit -m "feat(frontend): dashboard panel filter, panel badge, panel_id in mutations"
```

---

### Task 13: InboundForm — Target Panel Selection

**Files:**
- Modify: `frontend/src/components/inbound/InboundForm.tsx`

- [ ] **Step 1: Add "Target panel" dropdown**

When creating a new inbound (not editing), add a Select dropdown as the first field:

```tsx
const { data: panels } = useQuery<LinkedPanel[]>({
  queryKey: ['panels'],
  queryFn: async () => (await api.get<LinkedPanel[]>('/panels')).data,
});

const [targetPanelId, setTargetPanelId] = useState<number | null>(null);
```

Render the dropdown above protocol selection (only in create mode, not edit):

```tsx
{!isEdit && panels && panels.length > 0 && (
  <Select
    label="Target Panel"
    value={targetPanelId ?? 'local'}
    onChange={(e) => setTargetPanelId(e.target.value === 'local' ? null : Number(e.target.value))}
    options={[
      { value: 'local', label: 'Master (local)' },
      ...panels.map(p => ({ value: String(p.id), label: p.name })),
    ]}
  />
)}
```

- [ ] **Step 2: Pass panel_id in submit mutation**

In the mutation URL, append `?panel_id=N` when targeting a child panel:

```typescript
mutationFn: (data: any) => {
  const url = isEdit
    ? `/inbounds/${inbound!.tag}`
    : targetPanelId
      ? `/inbounds?panel_id=${targetPanelId}`
      : '/inbounds';
  return isEdit ? api.put(url, data) : api.post(url, data);
},
```

- [ ] **Step 3: Run typecheck**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/inbound/InboundForm.tsx
git commit -m "feat(frontend): InboundForm target panel selection for remote inbound creation"
```

---

### Task 14: TariffDrawer — Panel + Inbound Two-Step Selection

**Files:**
- Modify: `frontend/src/components/bot/TariffDrawer.tsx`
- Modify: `frontend/src/components/bot/TariffsTab.tsx`

- [ ] **Step 1: Update TariffsTab to fetch panels instead of nodes**

In `frontend/src/components/bot/TariffsTab.tsx`, replace the nodes query:

```typescript
// FROM:
const { data: nodes } = useQuery<Node[]>({ queryKey: ['nodes'], ... });
// TO:
const { data: panels } = useQuery<LinkedPanel[]>({
  queryKey: ['panels'],
  queryFn: async () => (await api.get<LinkedPanel[]>('/panels')).data,
});
```

Pass `panels` to `TariffDrawer` instead of `nodeGroups`.

- [ ] **Step 2: Update TariffDrawer FormItem type**

Replace `allowed_node_groups: string` with `panel_id: number | null`:

```typescript
interface FormItem {
  inbound_tag: string;
  label: string;
  traffic_gb: string;
  panel_id: number | null;
  sort_order: number;
}
```

- [ ] **Step 3: Update ItemRow — two-step selection**

In the `ItemRow` component (lines 514–661), replace the single inbound dropdown with two steps:

```tsx
// Step 1: Panel selection
<Select
  label="Panel"
  value={item.panel_id ?? 'local'}
  onChange={(e) => {
    const val = e.target.value === 'local' ? null : Number(e.target.value);
    onChange({ ...item, panel_id: val, inbound_tag: '' });
  }}
  options={[
    { value: 'local', label: 'Master (local)' },
    ...(panels || []).map(p => ({ value: String(p.id), label: p.name })),
  ]}
/>

// Step 2: Inbound selection (filtered by panel)
<Select
  label="Inbound"
  value={item.inbound_tag}
  onChange={(e) => onChange({ ...item, inbound_tag: e.target.value })}
  options={filteredInbounds}
/>
```

Where `filteredInbounds` filters the inbounds list by `panel_id`:

```typescript
const filteredInbounds = (allInbounds || [])
  .filter(ib => item.panel_id === null ? !ib.panel_id : ib.panel_id === item.panel_id)
  .map(ib => ({ value: ib.tag, label: ib.label || ib.tag }));
```

- [ ] **Step 4: Remove `allowed_node_groups` TagInput**

Delete the TagInput for node groups (lines ~629–639 in TariffDrawer.tsx) and the `nodeGroups` prop.

- [ ] **Step 5: Update handleSubmit to send panel_id**

In the submit handler, include `panel_id` in each item payload:

```typescript
items: formItems.map((item, i) => ({
  inbound_tag: item.inbound_tag,
  label: item.label,
  traffic_gb: Number(item.traffic_gb),
  panel_id: item.panel_id,
  sort_order: i,
})),
```

- [ ] **Step 6: Run typecheck and lint**

```bash
cd frontend && npx tsc --noEmit && npm run lint
```

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/bot/TariffDrawer.tsx frontend/src/components/bot/TariffsTab.tsx
git commit -m "feat(frontend): TariffDrawer two-step panel→inbound selection"
```

---

## Phase 3: Bot/Billing Adaptation

### Task 15: Notifications + Auto-Renewal with Panel Cache

**Files:**
- Modify: `backend/app/jobs/notifications.py`
- Modify: `backend/app/jobs/billing.py`

- [ ] **Step 1: Update expiry/traffic notifications to read from cache**

In `backend/app/jobs/notifications.py`, the `send_expiry_notifications` and `send_traffic_notifications` functions iterate `Client` rows. For child panel clients, the data lives in Redis cache instead.

Add a helper that builds a unified client list from local DB + cache:

```python
def _all_clients_with_tariff_access():
    """Local clients + cached child panel clients that have UserTariffAccess on this master."""
    from app.models import Client, UserTariffAccess, TariffItem, LinkedPanel
    from app.services.panel_proxy import get_panel_snapshot

    local_clients = Client.query.all()

    remote_clients = []
    remote_items = TariffItem.query.filter(TariffItem.panel_id.isnot(None)).all()
    for item in remote_items:
        snapshot = get_panel_snapshot(item.panel_id)
        if not snapshot:
            continue
        for ib in snapshot.get("inbounds", []):
            if ib["tag"] != item.inbound_tag:
                continue
            for c in ib.get("clients", []):
                remote_clients.append((item.panel_id, c))

    return local_clients, remote_clients
```

For the MVP, notifications only fire for local clients (the child panel's own bot handles its own notifications). This means no changes to notifications.py are strictly required — each panel's bot runs its own notification jobs against its own DB.

- [ ] **Step 2: Update auto_renew_free_users for remote TariffItems**

In `backend/app/jobs/billing.py`, the `auto_renew_free_users` function iterates `UserTariffAccess` rows with `billing='free'`. When the associated `TariffItem` has `panel_id`, route through proxy:

```python
# In the renewal loop, after loading the tariff:
for item in tariff.items:
    if item.panel_id is not None:
        from app.services.panel_proxy import proxy_provision
        try:
            proxy_provision(
                item.panel_id, access.telegram_id, item.inbound_tag,
                {"expiry_ms": new_expiry, "limit_bytes": item.traffic_gb * _GB if item.traffic_gb else 0,
                 "tariff_id": tariff.id},
            )
        except Exception as exc:
            logger.warning("auto_renew proxy failed panel=%s: %s", item.panel_id, exc)
            # Skip this cycle — retry in 15 min
            continue
```

- [ ] **Step 3: Run full test suite**

```bash
cd backend && python -m pytest tests/ -v
```

- [ ] **Step 4: Run all frontend checks**

```bash
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```

- [ ] **Step 5: Run all backend linting**

```bash
cd backend && ruff check . && ruff format --check .
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/jobs/
git commit -m "feat: auto-renewal routes through panel_proxy for remote TariffItems"
```

---

## Final Checklist

- [ ] **Run full CI checks locally**

```bash
ruff check backend/ tg_bot/ && ruff format --check backend/ tg_bot/
cd frontend && npx tsc --noEmit && npm run lint && npm run format:check && npm run build
cd ../backend && python -m pytest tests/ -v
```

- [ ] **Test manually with Docker**

```bash
docker compose build backend frontend && docker compose up -d
```

1. Open master panel → Panels page → verify "Add Panel" works
2. Open child panel → Panels page → "Generate Link Token"
3. Copy token to master → Add panel → verify handshake succeeds
4. Dashboard → verify child panel inbounds appear with badge
5. Create user on child inbound from master → verify it appears on child
6. Create tariff with child panel inbound → verify bot checkout works
7. Unlink panel → verify clean break

- [ ] **Final commit with all remaining fixes**

```bash
git add -A
git commit -m "feat: multi-panel federation — complete implementation"
```
