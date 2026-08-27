import logging
import os
import secrets
import time

from panel_core.extensions import db
from panel_core.models import FederationConfig, SystemSetting
from panel_core.services.master_client import (
    CLAIMED_SETTING_KEY,
    TRANSFER_TOKEN_ENV,
    MasterClient,
    decode_transfer_token,
)
from panel_core.services.node_identity import get_or_create_instance_id
from panel_core.services.state_apply import apply_state

logger = logging.getLogger(__name__)

PENDING_TOKEN_SETTING_KEY = "node_transfer_pending_federation_token"
RESYNC_PENDING_SETTING_KEY = "node_transfer_resync_pending"

_IDENTITY_FIELDS = (
    ("panel_domain", "PANEL_DOMAIN"),
    ("proxy_domain", "PROXY_DOMAIN"),
    ("secret_path", "PANEL_SECRET_PATH"),
)


def _get_setting(key: str) -> str:
    row = db.session.get(SystemSetting, key)
    return (row.value or "").strip() if row else ""


def _set_setting(key: str, value: str) -> None:
    row = db.session.get(SystemSetting, key)
    if row is None:
        db.session.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
    db.session.commit()


def _already_claimed() -> bool:
    return _get_setting(CLAIMED_SETTING_KEY) == "1"


def _mark_claimed() -> None:
    row = db.session.get(SystemSetting, CLAIMED_SETTING_KEY)
    if row is None:
        db.session.add(SystemSetting(key=CLAIMED_SETTING_KEY, value="1"))
    else:
        row.value = "1"
    pending = db.session.get(SystemSetting, PENDING_TOKEN_SETTING_KEY)
    if pending is not None:
        db.session.delete(pending)
    resync = db.session.get(SystemSetting, RESYNC_PENDING_SETTING_KEY)
    if resync is None:
        db.session.add(SystemSetting(key=RESYNC_PENDING_SETTING_KEY, value="1"))
    else:
        resync.value = "1"
    db.session.commit()


def _pending_federation_token() -> str:
    existing = _get_setting(PENDING_TOKEN_SETTING_KEY)
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    _set_setting(PENDING_TOKEN_SETTING_KEY, token)
    return token


def _check_identity_matches(cold: dict, master_url: str) -> None:
    identity = cold.get("identity") or {}
    mismatches = []
    for field, env_name in _IDENTITY_FIELDS:
        expected = (identity.get(field) or "").strip()
        actual = (os.environ.get(env_name) or "").strip()
        if expected and actual and expected != actual:
            mismatches.append(f"{env_name}={actual!r} (dead node had {expected!r})")
    if mismatches:
        logger.error(
            "state claimed from %s but this machine's identity does not match the dead node's: %s; "
            "the master will 404 every poll until .env is corrected and this container restarted",
            master_url,
            "; ".join(mismatches),
        )


def _resync_xray(master_url: str) -> None:
    from panel_core.xray.facade import generate_config_file, restart_xray_container

    ok = True
    try:
        generate_config_file()
    except Exception as exc:
        ok = False
        logger.error(
            "state claimed from %s but the Xray config could not be regenerated (%s); it will retry next tick",
            master_url,
            exc,
        )

    if ok:
        try:
            restart_xray_container()
        except Exception as exc:
            ok = False
            logger.error(
                "state claimed from %s but Xray could not be restarted (%s); it will retry next tick",
                master_url,
                exc,
            )

    _set_setting(RESYNC_PENDING_SETTING_KEY, "0" if ok else "1")


def claim_state_job():
    if _already_claimed():
        if _get_setting(RESYNC_PENDING_SETTING_KEY) == "1":
            cfg = db.session.get(FederationConfig, 1)
            _resync_xray(cfg.master_url if cfg and cfg.master_url else "this master")
        return

    raw = (os.environ.get(TRANSFER_TOKEN_ENV) or "").strip()
    if not raw:
        return

    try:
        master_url, secret = decode_transfer_token(raw)
    except ValueError as exc:
        logger.error("%s is set but unusable (%s); this node will not claim any state", TRANSFER_TOKEN_ENV, exc)
        return

    instance_id = get_or_create_instance_id()
    federation_token = _pending_federation_token()

    try:
        reply = MasterClient(master_url).claim(secret, instance_id, federation_token)
    except Exception as exc:
        logger.warning("state claim from %s failed (%s); retrying", master_url, exc)
        return

    cold = reply.get("cold") or {}
    apply_state(reply.get("hot") or {}, cold, carry_admin=bool(reply.get("carry_admin")))
    _check_identity_matches(cold, master_url)

    cfg = db.session.get(FederationConfig, 1)
    if cfg is None:
        cfg = FederationConfig(id=1)
        db.session.add(cfg)
    cfg.federation_token = federation_token
    cfg.master_url = master_url
    cfg.link_token = None
    cfg.link_token_used = True
    cfg.linked_at = int(time.time() * 1000)
    db.session.commit()

    _mark_claimed()
    _resync_xray(master_url)

    logger.warning(
        "state claimed from %s and applied; move the A record to this machine so Caddy can issue a certificate",
        master_url,
    )


_check_failure_warned: set[str] = set()


def supersede_check_job():
    from panel_core.services.supersede import clear_superseded, is_superseded, mark_superseded

    cfg = db.session.get(FederationConfig, 1)
    if cfg is None or not (cfg.master_url or "").strip() or not (cfg.federation_token or "").strip():
        return

    instance_id = get_or_create_instance_id()
    try:
        reply = MasterClient(cfg.master_url).instance_check(cfg.federation_token, instance_id)
    except Exception as exc:
        logger.debug("instance check against %s failed (%s); carrying on as usual", cfg.master_url, exc)
        if cfg.master_url not in _check_failure_warned:
            _check_failure_warned.add(cfg.master_url)
            logger.warning(
                "instance check against %s has been failing (%s); this node cannot yet tell whether it "
                "has been superseded and keeps working as usual until it can reach the master again",
                cfg.master_url,
                exc,
            )
        return

    _check_failure_warned.discard(cfg.master_url)

    if not isinstance(reply, dict):
        return

    verdict = reply.get("verdict")

    if verdict == "superseded":
        if not is_superseded():
            try:
                superseded_at_ms = int(reply.get("superseded_at") or 0)
            except (TypeError, ValueError):
                superseded_at_ms = 0
            mark_superseded(superseded_at_ms)
            logger.error(
                "this installation has been superseded on the master; notifications are now muted and the "
                "background jobs that emit them are stopped. Traffic keeps flowing for whoever still reaches "
                "this machine — shut it down once the A record has settled"
            )
        return

    if verdict == "current" and is_superseded():
        clear_superseded()
        logger.warning(
            "this installation is current on the master again; resuming as usual after having been superseded"
        )
