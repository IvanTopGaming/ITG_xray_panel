import json
from pathlib import Path

from panel_core.app_base import _require_schema
from panel_core.extensions import db
from panel_core.models import RuntimeApplyState
from panel_core.services.runtime_apply import runtime_lock
from panel_core.xray import engine, grpc_client


def _running_identity():
    client = engine.docker.from_env(timeout=3)
    try:
        attrs = client.api.inspect_container(engine.XRAY_CONTAINER_NAME)
    finally:
        client.close()
    state = attrs.get("State", {})
    identity = (
        attrs.get("Id"),
        attrs.get("Image"),
        state.get("Pid"),
        state.get("StartedAt"),
        attrs.get("RestartCount"),
    )
    if (
        state.get("Running") is not True
        or state.get("Paused") is not False
        or state.get("Restarting") is not False
        or not all(identity[:4])
        or not isinstance(identity[2], int)
        or identity[2] <= 0
        or not isinstance(identity[3], str)
        or identity[3].startswith("0001-")
        or not isinstance(identity[4], int)
        or identity[4] < 0
    ):
        raise RuntimeError("Cannot preserve Xray: running identity is unavailable")
    return identity


def preserve_existing_runtime():
    with runtime_lock(engine.LOCK_PATH, timeout=5):
        _require_schema()
        state = db.session.get(RuntimeApplyState, 1, populate_existing=True)
        if (
            state is None
            or state.desired_revision != state.applied_revision
            or state.desired_revision < 0
            or state.last_error
        ):
            raise RuntimeError("Cannot preserve Xray: existing runtime state is not clean")
        revision = state.desired_revision
        identity = _running_identity()
        path = Path(engine.CONFIG_PATH)
        published = path.read_bytes()
        original = json.loads(published)
        candidate = engine.preview_validated_config()
        if json.dumps(candidate, sort_keys=True, allow_nan=False) != json.dumps(
            original, sort_keys=True, allow_nan=False
        ):
            raise RuntimeError("Cannot preserve Xray: generated configuration differs from published configuration")
        epoch, _ = grpc_client.read_traffic_counters()
        if _running_identity() != identity or epoch != identity[3]:
            raise RuntimeError("Cannot preserve Xray: running identity changed during startup")
        if path.read_bytes() != published:
            raise RuntimeError("Cannot preserve Xray: published configuration changed during startup")
        state = db.session.get(RuntimeApplyState, 1, populate_existing=True)
        if (
            state is None
            or state.desired_revision != revision
            or state.applied_revision != revision
            or state.last_error
        ):
            raise RuntimeError("Cannot preserve Xray: runtime state changed during startup")
