import base64
import json
import logging
import time

import gevent.pool
import requests

from sqlalchemy import text

from app.extensions import db, scheduler
from app.models import Node, Inbound
from app.services.xray import flatten_stream_settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10
HEALTH_TIMEOUT = 5


def _is_error(payload):
    return isinstance(payload, dict) and "error" in payload


class NodeClient:
    """Synchronous HTTP client for a single remote Xray panel node."""

    def __init__(self, node):
        self.node = node
        self.base_url = node.url.rstrip("/")
        self.token = None
        self.session = requests.Session()

    def login(self):
        try:
            resp = self.session.post(
                f"{self.base_url}/api/login",
                json={"username": self.node.username, "password": self.node.password},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                self.token = resp.json().get("token")
                return True
            logger.warning("[%s] Login failed: %s", self.node.name, resp.status_code)
            return False
        except Exception as e:
            logger.warning("[%s] Login error: %s", self.node.name, e)
            return False

    def request(self, method, endpoint, **kwargs):
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        if not self.token:
            if not self.login():
                return {"error": "Auth failed"}, 0

        url = f"{self.base_url}/api/{endpoint}"
        headers = {"Authorization": f"Bearer {self.token}"}

        try:
            resp = self.session.request(method, url, headers=headers, **kwargs)
        except requests.RequestException as e:
            return {"error": str(e)}, 0

        if resp.status_code == 401:
            if not self.login():
                return {"error": "Auth failed"}, 0
            headers = {"Authorization": f"Bearer {self.token}"}
            try:
                resp = self.session.request(method, url, headers=headers, **kwargs)
            except requests.RequestException as e:
                return {"error": str(e)}, 0

        try:
            body = resp.json() if "json" in resp.headers.get("content-type", "") else resp.text
        except Exception:
            body = resp.text
        return body, resp.status_code

    def health_check(self):
        start = time.monotonic()
        try:
            resp = self.session.get(f"{self.base_url}/healthz", timeout=HEALTH_TIMEOUT)
            latency = int((time.monotonic() - start) * 1000)
            return {"online": resp.status_code == 200, "latency_ms": latency, "error": ""}
        except Exception as e:
            latency = int((time.monotonic() - start) * 1000)
            return {"online": False, "latency_ms": latency, "error": str(e)}

    def add_user(self, inbound_tag, user_data):
        return self.request("POST", f"inbounds/{inbound_tag}/users", json=user_data)

    def update_user(self, inbound_tag, user_data):
        return self.request("PUT", f"inbounds/{inbound_tag}/users", json=user_data)

    def delete_user(self, inbound_tag, email):
        return self.request("DELETE", f"inbounds/{inbound_tag}/users", params={"email": email})

    def get_inbounds(self):
        body, status = self.request("GET", "inbounds")
        if status == 200 and isinstance(body, list):
            return body
        return None

    def upsert_inbound(self, payload):
        """Push an inbound config to the remote node. PUT first; on 404, fall back to POST."""
        tag = payload.get("tag")
        body, status = self.request("PUT", f"inbounds/{tag}", json=payload)
        if status == 404:
            body, status = self.request("POST", "inbounds", json=payload)
        return body, status

    def create_inbound_if_missing(self, payload):
        """Create the inbound only if it does not exist on the remote node.

        Returns (body, status, created) — created=False means the inbound was already
        present and was left untouched.
        """
        tag = payload.get("tag")
        existing = self.get_inbounds()
        if existing is None:
            return {"error": "Failed to fetch remote inbounds"}, 0, False
        if any(ib.get("tag") == tag for ib in existing):
            return {"status": "exists"}, 200, False
        body, status = self.request("POST", "inbounds", json=payload)
        return body, status, status in (200, 201)

    def delete_inbound(self, tag):
        body, status = self.request("DELETE", f"inbounds/{tag}")
        if status == 404:
            return body, 200
        return body, status

    def get_subscription_raw(self, uuid_str, user_agent="v2ray"):
        """Fetch raw subscription from a remote node with specified User-Agent."""
        try:
            resp = self.session.get(
                f"{self.base_url}/api/sub/{uuid_str}",
                headers={"User-Agent": user_agent},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.text
        except Exception as e:
            logger.warning("[%s] Sub fetch error: %s", self.node.name, e)
        return None


def _node_groups(node):
    return {g.strip() for g in (node.groups or "").split(",") if g.strip()}


def _client_allowed_groups(client):
    if client is None:
        return set()
    raw = getattr(client, "allowed_node_groups", "") or ""
    return {g.strip() for g in raw.split(",") if g.strip()}


def _node_visible_to_client(node, allowed_groups):
    """Return True if a client with the given allowed group set may see this node.

    - empty allowed_groups → no filter, all nodes visible.
    - non-empty allowed_groups → node must have at least one matching group tag.
      A node with no groups is treated as 'common' and is always visible.
    """
    if not allowed_groups:
        return True
    node_groups = _node_groups(node)
    if not node_groups:
        return True
    return bool(node_groups & allowed_groups)


def _get_sync_nodes():
    return Node.query.filter_by(enable=True, sync_users=True).all()


def _get_active_nodes(client=None):
    nodes = Node.query.filter_by(enable=True).all()
    if client is None:
        return nodes
    allowed = _client_allowed_groups(client)
    if not allowed:
        return nodes
    return [n for n in nodes if _node_visible_to_client(n, allowed)]


def _get_inbound_sync_nodes(inbound_tag=None):
    q = Node.query.filter_by(enable=True, sync_inbound=True)
    if inbound_tag is not None:
        q = q.filter_by(inbound_tag=inbound_tag)
    return q.all()


def _inbound_payload(ib):
    """Build the flat-key dict that POST/PUT /api/inbounds expects.

    Excludes routing_profile_id and clients on purpose: routing is master-side, and
    users are propagated by the existing sync_user_* hooks / reconciler.
    """
    try:
        stored_stream = json.loads(ib.stream_settings or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        stored_stream = {}
    flat = flatten_stream_settings(stored_stream, ib.protocol)
    payload = {
        "tag": ib.tag,
        "port": ib.port,
        "protocol": ib.protocol,
        "fallback_address": ib.fallback_address or "",
    }
    payload.update(flat)
    return payload


def sync_user_create(email, user_data):
    """Fire-and-forget: create user on all sync-enabled nodes."""
    nodes = _get_sync_nodes()
    if not nodes:
        return

    def _do(node):
        client = NodeClient(node)
        body, status = client.add_user(node.inbound_tag, user_data)
        if status not in (200, 201) and not (isinstance(body, dict) and "Email exists" in str(body.get("error", ""))):
            logger.warning("[%s] sync create %s failed: %s", node.name, email, body)

    pool = gevent.pool.Pool(size=min(len(nodes), 10))
    pool.map(_do, nodes)


def sync_user_update(old_email, update_data):
    """Fire-and-forget: update user on all sync-enabled nodes."""
    nodes = _get_sync_nodes()
    if not nodes:
        return

    def _do(node):
        data = {**update_data, "tag": node.inbound_tag}
        client = NodeClient(node)
        body, status = client.update_user(node.inbound_tag, data)
        if status != 200:
            logger.warning("[%s] sync update %s failed: %s", node.name, old_email, body)

    pool = gevent.pool.Pool(size=min(len(nodes), 10))
    pool.map(_do, nodes)


def sync_user_delete(email):
    """Fire-and-forget: delete user from all sync-enabled nodes."""
    nodes = _get_sync_nodes()
    if not nodes:
        return

    def _do(node):
        client = NodeClient(node)
        body, status = client.delete_user(node.inbound_tag, email)
        if status != 200 and not (isinstance(body, dict) and "not found" in str(body.get("error", "")).lower()):
            logger.warning("[%s] sync delete %s failed: %s", node.name, email, body)

    pool = gevent.pool.Pool(size=min(len(nodes), 10))
    pool.map(_do, nodes)


def get_aggregated_sub_links(client_email, client=None):
    """Fetch subscription links from all active nodes for a given user email.

    If `client` is provided, nodes are filtered by the client's allowed_node_groups.
    """
    nodes = _get_active_nodes(client=client)
    if not nodes:
        return []

    all_links = []

    def _fetch(node):
        client = NodeClient(node)
        inbounds = client.get_inbounds()
        if not inbounds:
            return []
        for ib in inbounds:
            if ib.get("tag") != node.inbound_tag:
                continue
            clients = ib.get("settings", {}).get("clients", [])
            for c in clients:
                if c.get("email") == client_email:
                    raw = client.get_subscription_raw(c["id"])
                    if raw:
                        try:
                            decoded = base64.b64decode(raw).decode("utf-8")
                            links = [ln for ln in decoded.strip().splitlines() if ln.strip()]
                            tagged = []
                            for link in links:
                                if "#" in link:
                                    base, _ = link.rsplit("#", 1)
                                    tagged.append(f"{base}#{node.name}")
                                else:
                                    tagged.append(f"{link}#{node.name}")
                            return tagged
                        except Exception:
                            return [raw]
            break
        return []

    pool = gevent.pool.Pool(size=min(len(nodes), 10))
    results = pool.map(_fetch, nodes)
    for links in results:
        all_links.extend(links)
    return all_links


def get_remote_configs(client_email, config_type="clash", client=None):
    """Fetch full Clash or sing-box configs from all active nodes.

    Returns list of (node_name, raw_config_text) tuples. If `client` is given,
    nodes are filtered by allowed_node_groups.
    """
    user_agent = "clash-meta" if config_type == "clash" else "sing-box"
    nodes = _get_active_nodes(client=client)
    if not nodes:
        return []

    results = []

    def _fetch(node):
        client = NodeClient(node)
        inbounds = client.get_inbounds()
        if not inbounds:
            return None
        for ib in inbounds:
            if ib.get("tag") != node.inbound_tag:
                continue
            for c in ib.get("settings", {}).get("clients", []):
                if c.get("email") == client_email:
                    raw = client.get_subscription_raw(c["id"], user_agent=user_agent)
                    if raw:
                        return (node.name, raw)
            break
        return None

    pool = gevent.pool.Pool(size=min(len(nodes), 10))
    for result in pool.map(_fetch, nodes):
        if result:
            results.append(result)
    return results


def node_health_check_job():
    """Scheduler job: update health status for all enabled nodes."""
    with scheduler.app.app_context():
        nodes = Node.query.filter_by(enable=True).all()
        if not nodes:
            return
        now_ms = int(time.time() * 1000)

        def _check(node):
            client = NodeClient(node)
            result = client.health_check()
            node.status = "online" if result["online"] else "offline"
            node.last_check = now_ms
            node.last_error = result.get("error", "")

        pool = gevent.pool.Pool(size=min(len(nodes), 10))
        pool.map(_check, nodes)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Failed to commit node health check results")


def _client_drifted(local_c, remote):
    """Check whether a local Client differs from the remote-side user dict."""
    return (
        str(local_c.id or "") != str(remote.get("id") or "")
        or int(local_c.limit_bytes or 0) != int(remote.get("limit_bytes") or 0)
        or int(local_c.expiry_time or 0) != int(remote.get("expiry_time") or 0)
        or bool(local_c.enable) != bool(remote.get("enable"))
        or (local_c.flow or "") != (remote.get("flow") or "")
        or int(local_c.reset_day or 0) != int(remote.get("reset_day") or 0)
    )


def _collect_local_clients():
    local_clients = {}
    for ib in Inbound.query.all():
        for c in ib.clients:
            local_clients[c.email] = c
    return local_clients


def reconcile_users_on_node(node, local_clients=None):
    """Reconcile users on a single node against the master.

    Returns a dict {added, updated, deleted, failed}. Master is the source of truth:
    missing users are added, drifted users are updated, and (if strict_mirror) extras
    are deleted. Caller is responsible for app context.
    """
    stats = {"added": 0, "updated": 0, "deleted": 0, "failed": 0}

    if local_clients is None:
        local_clients = _collect_local_clients()
    if not local_clients:
        return stats

    client = NodeClient(node)
    inbounds = client.get_inbounds()
    if not inbounds:
        stats["failed"] = 1
        return stats

    remote_users = {}
    for ib in inbounds:
        if ib.get("tag") != node.inbound_tag:
            continue
        for rc in ib.get("settings", {}).get("clients", []):
            email = rc.get("email")
            if email:
                remote_users[email] = rc
        break

    for email, local_c in local_clients.items():
        if email not in remote_users:
            user_data = {
                "email": email,
                "id": local_c.id,
                "limit_bytes": local_c.limit_bytes,
                "expiry_time": local_c.expiry_time,
                "enable": local_c.enable,
                "reset_day": local_c.reset_day or 0,
                "flow": local_c.flow or "",
            }
            body, status = client.add_user(node.inbound_tag, user_data)
            if status in (200, 201):
                stats["added"] += 1
            else:
                stats["failed"] += 1
                logger.warning("[%s] Failed to add user %s: %s", node.name, email, body)
            continue

        remote = remote_users[email]
        if not _client_drifted(local_c, remote):
            continue

        update_data = {
            "old_email": email,
            "new_email": email,
            "new_id": local_c.id,
            "limit_bytes": local_c.limit_bytes,
            "expiry_time": local_c.expiry_time,
            "enable": local_c.enable,
            "reset_day": local_c.reset_day or 0,
            "flow": local_c.flow or "",
        }
        body, status = client.update_user(node.inbound_tag, update_data)
        if status == 200:
            stats["updated"] += 1
        else:
            stats["failed"] += 1
            logger.warning("[%s] Failed to update user %s: %s", node.name, email, body)

    if node.strict_mirror:
        for email in list(remote_users.keys()):
            if email in local_clients:
                continue
            body, status = client.delete_user(node.inbound_tag, email)
            if status in (200, 204) or (isinstance(body, dict) and "not found" in str(body.get("error", "")).lower()):
                stats["deleted"] += 1
            else:
                stats["failed"] += 1
                logger.warning(
                    "[%s] strict_mirror: failed to delete orphan %s: %s",
                    node.name,
                    email,
                    body,
                )

    if any(stats.values()):
        logger.info(
            "[%s] Reconcile: +%d added, ~%d updated, -%d deleted, %d failed",
            node.name,
            stats["added"],
            stats["updated"],
            stats["deleted"],
            stats["failed"],
        )
    return stats


def node_user_sync_job():
    """Scheduler job: reconcile users — ensure all sync nodes mirror the master.

    For each sync-enabled node we:
      * add users that exist on master but not on the node
      * update users that exist on both but whose id/limit/expiry/enable/flow/reset_day drifted

    Master is the source of truth. Extra users on the node (not present on master)
    are intentionally left alone — deletion is handled by explicit user-delete sync.
    """
    with scheduler.app.app_context():
        nodes = _get_sync_nodes()
        if not nodes:
            return

        local_clients = _collect_local_clients()
        if not local_clients:
            return

        for node in nodes:
            try:
                reconcile_users_on_node(node, local_clients=local_clients)
            except Exception:
                logger.exception("[%s] User sync failed", node.name)


def node_traffic_poll_job():
    """Scheduler job: poll each node for per-user traffic counters and persist them.

    The remote /api/inbounds endpoint already returns per-client up/down totals.
    We store the latest absolute values per (node, email) so the master can:
      * sum across all nodes for a true global usage view
      * enforce a per-user global_limit_bytes that combines master + node usage
    """
    with scheduler.app.app_context():
        nodes = _get_active_nodes()
        if not nodes:
            return
        now_ms = int(time.time() * 1000)

        def _poll(node):
            client = NodeClient(node)
            inbounds = client.get_inbounds()
            if not inbounds:
                return []
            samples = []
            for ib in inbounds:
                if ib.get("tag") != node.inbound_tag:
                    continue
                for rc in ib.get("settings", {}).get("clients", []) or []:
                    email = rc.get("email")
                    if not email:
                        continue
                    samples.append((email, int(rc.get("up") or 0), int(rc.get("down") or 0)))
                break
            return [(node.id, e, u, d) for (e, u, d) in samples]

        pool = gevent.pool.Pool(size=min(len(nodes), 10))
        results = pool.map(_poll, nodes)

        upserts = 0
        for node_samples in results:
            for node_id, email, up, down in node_samples:
                db.session.execute(
                    text(
                        """
                        INSERT INTO node_client_traffic (node_id, email, up, down, last_polled)
                        VALUES (:nid, :email, :up, :down, :ts)
                        ON CONFLICT(node_id, email) DO UPDATE SET
                            up          = excluded.up,
                            down        = excluded.down,
                            last_polled = excluded.last_polled
                        """
                    ),
                    {"nid": node_id, "email": email, "up": up, "down": down, "ts": now_ms},
                )
                upserts += 1
        if upserts:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception("Failed to commit node traffic poll")


def sync_inbound_to_all_nodes(ib):
    """Fire-and-forget: create the inbound on every sync-enabled node whose
    inbound_tag matches this inbound's tag — but only if the inbound is missing
    on the remote node. Existing remote inbounds are left untouched; admins must
    use the explicit Force Push action to overwrite a node-side inbound."""
    nodes = _get_inbound_sync_nodes(inbound_tag=ib.tag)
    if not nodes:
        return

    try:
        payload = _inbound_payload(ib)
    except ValueError as e:
        logger.warning("[inbound %s] skipped sync: %s", ib.tag, e)
        return

    def _do(node):
        client = NodeClient(node)
        body, status, created = client.create_inbound_if_missing(payload)
        if created:
            logger.info("[%s] sync inbound %s: created", node.name, ib.tag)
        elif status not in (200, 201):
            logger.warning("[%s] sync inbound %s failed: %s", node.name, ib.tag, body)

    pool = gevent.pool.Pool(size=min(len(nodes), 10))
    pool.map(_do, nodes)


def sync_inbound_delete_to_all_nodes(tag):
    """Fire-and-forget: delete the inbound on every sync-enabled node bound to this tag."""
    nodes = _get_inbound_sync_nodes(inbound_tag=tag)
    if not nodes:
        return

    def _do(node):
        client = NodeClient(node)
        body, status = client.delete_inbound(tag)
        if status not in (200, 204):
            logger.warning("[%s] sync delete inbound %s failed: %s", node.name, tag, body)

    pool = gevent.pool.Pool(size=min(len(nodes), 10))
    pool.map(_do, nodes)


def sync_inbound_to_node(node, ib, force=False):
    """Synchronous single-node push for the manual REST trigger.

    Returns (body, status, overwritten). When force=False, the remote inbound is
    left alone if it already exists on the node — only missing inbounds are created.
    When force=True, the remote inbound is overwritten unconditionally.
    """
    try:
        payload = _inbound_payload(ib)
    except ValueError as e:
        return {"error": str(e)}, 400, False
    client = NodeClient(node)
    if force:
        body, status = client.upsert_inbound(payload)
        return body, status, status in (200, 201)
    body, status, created = client.create_inbound_if_missing(payload)
    return body, status, created


def node_inbound_sync_job():
    """Scheduler job: ensure each sync-enabled node has its inbound — non-destructive.

    If the master has an inbound matching the node's inbound_tag and the node is
    missing it, create it on the node. Existing remote inbounds are NEVER touched
    or deleted by this job — overwrites are reserved for the explicit Force Push
    action so that node-side tweaks survive periodic reconciles.
    """
    with scheduler.app.app_context():
        nodes = _get_inbound_sync_nodes()
        if not nodes:
            return

        for node in nodes:
            try:
                ib = Inbound.query.filter_by(tag=node.inbound_tag).first()
                if ib is None:
                    continue

                try:
                    payload = _inbound_payload(ib)
                except ValueError as e:
                    logger.warning("[%s] Reconcile: skipped %s: %s", node.name, ib.tag, e)
                    continue

                client = NodeClient(node)
                body, status, created = client.create_inbound_if_missing(payload)
                if created:
                    logger.info("[%s] Reconcile: created inbound %s", node.name, ib.tag)
                elif status not in (200, 201):
                    logger.warning(
                        "[%s] Reconcile: failed to create inbound %s: %s",
                        node.name,
                        ib.tag,
                        body,
                    )
            except Exception:
                logger.exception("[%s] Inbound sync failed", node.name)
