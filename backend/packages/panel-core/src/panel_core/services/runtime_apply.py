import fcntl
import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from flask import current_app, g, has_app_context
from sqlalchemy import update

from panel_core.extensions import db
from panel_core.models import RuntimeApplyState
from panel_core.xray.facade import generate_config_file, has_local_xray, restart_xray_container

DEFAULT_LOCK_PATH = "/etc/xray/config.lock"
_held_locks = ContextVar("runtime_apply_locks", default=None)
logger = logging.getLogger(__name__)


class RuntimeApplyError(RuntimeError):
    pass


@contextmanager
def runtime_lock(path=None, *, timeout=30):
    configured_path = current_app.config.get("XRAY_CONFIG_LOCK_PATH") if has_app_context() else None
    path = os.path.abspath(path or configured_path or DEFAULT_LOCK_PATH)
    owner = (os.getpid(), threading.get_ident(), id(g._get_current_object()) if has_app_context() else None)
    held = _held_locks.get() or {}
    if held.get(path) == owner:
        yield
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    token = None
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting to apply Xray configuration")
                time.sleep(0.01)
        token = _held_locks.set({**held, path: owner})
        yield
    finally:
        if token is not None:
            _held_locks.reset(token)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def runtime_mutation(handler):
    @wraps(handler)
    def guarded(*args, **kwargs):
        if not has_local_xray():
            return handler(*args, **kwargs)
        with runtime_lock():
            return handler(*args, **kwargs)

    return guarded


def mark_runtime_dirty():
    dialect = db.session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise RuntimeError(f"Unsupported runtime state database: {dialect}")
    db.session.execute(
        insert(RuntimeApplyState)
        .values(id=1, desired_revision=0, applied_revision=0, last_error="")
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return db.session.execute(
        update(RuntimeApplyState)
        .where(RuntimeApplyState.id == 1)
        .values(desired_revision=RuntimeApplyState.desired_revision + 1)
        .returning(RuntimeApplyState.desired_revision)
    ).scalar_one()


def prepare_runtime_config():
    with runtime_lock():
        generate_config_file(publish=False)


def synchronize_runtime(callback=None, *, expected_revision=None):
    with runtime_lock():
        state = db.session.get(RuntimeApplyState, 1, populate_existing=True)
        if state is None or state.desired_revision == state.applied_revision:
            return False
        revision = state.desired_revision
        may_apply_partial = callback is not None and expected_revision == revision == state.applied_revision + 1
        try:
            generate_config_file()
            if not may_apply_partial or callback() is False:
                _settle_before_restart()
                restart_xray_container()
        except Exception as exc:
            db.session.rollback()
            db.session.execute(
                update(RuntimeApplyState)
                .where(RuntimeApplyState.id == 1, RuntimeApplyState.desired_revision == revision)
                .values(last_error=str(exc)[:1000])
            )
            db.session.commit()
            raise RuntimeApplyError("Xray changes were saved but could not be applied; recovery is pending") from exc
        result = db.session.execute(
            update(RuntimeApplyState)
            .where(RuntimeApplyState.id == 1, RuntimeApplyState.desired_revision == revision)
            .values(applied_revision=revision, last_error="")
        )
        db.session.commit()
        return bool(result.rowcount)


def retry_pending_runtime():
    return synchronize_runtime()


def _settle_before_restart():
    from panel_core.models import Client, Inbound
    from panel_core.services.traffic_store import read_traffic_sample, settle_client_traffic, settle_inbound_traffic

    try:
        sample = read_traffic_sample()
        if sample is None:
            return
        for client in Client.query.all():
            if client.up is not None and client.down is not None and client.up >= 0 and client.down >= 0:
                settle_client_traffic(client, sample=sample)
        for inbound in Inbound.query.all():
            settle_inbound_traffic(inbound, sample=sample)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.error(
            "Restarting Xray without a final traffic sample; the uncollected runtime interval may be lost",
            exc_info=True,
        )
