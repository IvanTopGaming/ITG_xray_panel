import base64
import hmac
import os
import secrets
import time
from urllib.parse import urlparse

from panel_core.extensions import db
from panel_core.models import LinkedPanel
from panel_core.services.panel_proxy import FederationClient
from panel_core.services.state_mirror import load_state, read_current, write_cold, write_hot

TRANSFER_TOKEN_TTL_SECONDS = 3600
CLAIMED_TOKEN_GRACE_SECONDS = 300
_SHRINK_RATIO = 0.5


class TransferError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _master_url() -> str:
    domain = (os.environ.get("PANEL_DOMAIN") or "").strip()
    secret = (os.environ.get("PANEL_SECRET_PATH") or "").strip()
    if not domain or not secret:
        raise ValueError("PANEL_DOMAIN and PANEL_SECRET_PATH must both be set to issue a transfer key")
    return f"https://{domain}/{secret}"


def _client_count(inbounds) -> int:
    return sum(len(ib.get("clients") or []) for ib in inbounds)


def _shrink_flagged(panel_id, inbounds) -> bool:
    existing = read_current(panel_id)
    if existing is None or not existing.hot_state:
        return False
    previous = load_state(existing)[0].get("inbounds") or []
    was, now = _client_count(previous), _client_count(inbounds)
    return bool(was and now < was * _SHRINK_RATIO)


def refresh_mirror_live(panel) -> dict:
    try:
        payload = FederationClient(panel.url, panel.federation_token).state()
        hot = payload.get("hot") if isinstance(payload, dict) else None
        inbounds = hot.get("inbounds") if isinstance(hot, dict) else None
        if not isinstance(inbounds, list):
            raise ValueError("federation state reply carries no usable inbounds list")

        now = int(time.time() * 1000)
        write_hot(
            panel.id,
            {"inbounds": inbounds},
            taken_at=now,
            instance_id=payload.get("instance_id") or "",
            app_version=payload.get("app_version") or "",
            shrink_flagged=_shrink_flagged(panel.id, inbounds),
        )
        write_cold(panel.id, payload.get("cold") or {}, fingerprint=payload.get("fingerprint") or "", taken_at=now)
        return {"ok": True, "taken_at": now, "error": None}
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        row = read_current(panel.id)
        return {"ok": False, "taken_at": row.hot_updated_at if row else None, "error": str(exc)[:200]}


def issue_transfer_token(panel, *, carry_admin: bool) -> dict:
    master_url = _master_url()

    freshness = refresh_mirror_live(panel)

    raw = secrets.token_urlsafe(32)
    panel.transfer_token = raw
    panel.transfer_token_expires_at = int(time.time() * 1000) + TRANSFER_TOKEN_TTL_SECONDS * 1000
    panel.transfer_token_used = False
    panel.transfer_claimed_instance_id = None
    panel.transfer_carry_admin = bool(carry_admin)
    db.session.commit()

    composite = base64.urlsafe_b64encode(f"{master_url}|{raw}".encode()).decode().rstrip("=")
    return {"token": composite, "expires_at": panel.transfer_token_expires_at, "state_freshness": freshness}


def _panel_for_token(raw_token: str) -> LinkedPanel:
    raw_token = (raw_token or "").strip()
    if not raw_token:
        raise TransferError("transfer token is required", 401)
    if not raw_token.isascii():
        raise TransferError("unknown transfer token", 401)

    for panel in LinkedPanel.query.filter(LinkedPanel.transfer_token.isnot(None)).all():
        if hmac.compare_digest(panel.transfer_token or "", raw_token):
            if (panel.transfer_token_expires_at or 0) < int(time.time() * 1000):
                raise TransferError("this transfer token has expired", 401)
            return panel

    raise TransferError("unknown transfer token", 401)


def resolve_identity(raw_token: str) -> dict:
    panel = _panel_for_token(raw_token)
    row = read_current(panel.id)
    identity = {}
    if row is not None:
        _, cold = load_state(row)
        identity = cold.get("identity") or {}

    parsed = urlparse(panel.url or "")
    if not identity.get("secret_path"):
        identity["secret_path"] = (parsed.path or "").strip("/")
    if not identity.get("panel_domain"):
        identity["panel_domain"] = parsed.hostname or ""

    return {
        "panel_name": panel.name,
        "panel_domain": identity.get("panel_domain") or "",
        "proxy_domain": identity.get("proxy_domain") or "",
        "secret_path": identity.get("secret_path") or "",
    }


def _state_reply(panel) -> dict:
    row = read_current(panel.id)
    if row is None:
        raise TransferError("no state mirrored for this panel yet", 409)
    hot, cold = load_state(row)
    if not panel.transfer_carry_admin:
        cold.pop("admin", None)
    return {
        "panel_name": panel.name,
        "hot": hot,
        "cold": cold,
        "carry_admin": bool(panel.transfer_carry_admin),
        "taken_at": row.hot_updated_at,
        "node_app_version": row.node_app_version,
    }


def instance_verdict(federation_token: str, instance_id: str) -> dict:
    federation_token = (federation_token or "").strip()
    instance_id = (instance_id or "").strip()
    if not federation_token or not instance_id:
        return {"verdict": "unknown", "superseded_at": None}

    for panel in LinkedPanel.query.all():
        if hmac.compare_digest(panel.federation_token or "", federation_token) and hmac.compare_digest(
            panel.current_instance_id or "", instance_id
        ):
            return {"verdict": "current", "superseded_at": None}

    for panel in LinkedPanel.query.filter(LinkedPanel.superseded_token.isnot(None)).all():
        if not hmac.compare_digest(panel.superseded_token or "", federation_token):
            continue
        if panel.superseded_instance_id and not hmac.compare_digest(panel.superseded_instance_id, instance_id):
            continue
        return {"verdict": "superseded", "superseded_at": panel.superseded_at}

    return {"verdict": "unknown", "superseded_at": None}


def claim_transfer(raw_token: str, *, instance_id: str, federation_token: str) -> dict:
    raw_token = (raw_token or "").strip()
    instance_id = (instance_id or "").strip()
    federation_token = (federation_token or "").strip()
    if not instance_id or not federation_token:
        raise TransferError("instance_id and federation_token are required", 400)

    panel = _panel_for_token(raw_token)

    if panel.transfer_token_used:
        if hmac.compare_digest(panel.transfer_claimed_instance_id or "", instance_id):
            return _state_reply(panel)
        raise TransferError("this transfer token has already been claimed by another node", 409)

    now = int(time.time() * 1000)
    mirror_row = read_current(panel.id)
    claimed = LinkedPanel.query.filter(
        LinkedPanel.id == panel.id,
        LinkedPanel.transfer_token == raw_token,
        LinkedPanel.transfer_token_used.is_(False),
    ).update(
        {
            "transfer_token_used": True,
            "transfer_claimed_instance_id": instance_id,
            "superseded_token": panel.federation_token,
            "superseded_instance_id": panel.current_instance_id
            or (mirror_row.node_instance_id if mirror_row else None),
            "superseded_at": now,
            "federation_token": federation_token,
            "current_instance_id": instance_id,
            "transfer_state": "awaiting_dns",
            "transfer_token_expires_at": min(
                panel.transfer_token_expires_at or now,
                now + CLAIMED_TOKEN_GRACE_SECONDS * 1000,
            ),
        },
        synchronize_session=False,
    )
    db.session.commit()

    if claimed != 1:
        db.session.refresh(panel)
        if hmac.compare_digest(panel.transfer_claimed_instance_id or "", instance_id):
            return _state_reply(panel)
        raise TransferError("this transfer token has already been claimed by another node", 409)

    db.session.refresh(panel)
    return _state_reply(panel)
