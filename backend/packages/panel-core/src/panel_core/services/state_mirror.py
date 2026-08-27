import json

from panel_core.extensions import db
from panel_core.models import PanelStateMirror

KIND_CURRENT = "current"
KIND_DAILY = "daily"


def read_current(panel_id: int) -> PanelStateMirror | None:
    return PanelStateMirror.query.filter_by(panel_id=panel_id, kind=KIND_CURRENT).first()


def _current_or_new(panel_id: int) -> PanelStateMirror:
    row = read_current(panel_id)
    if row is None:
        row = PanelStateMirror(panel_id=panel_id, kind=KIND_CURRENT, taken_at=0)
        db.session.add(row)
    return row


def write_hot(
    panel_id: int, hot: dict, *, taken_at: int, instance_id: str, app_version: str, shrink_flagged: bool
) -> None:
    row = _current_or_new(panel_id)
    row.hot_state = json.dumps(hot, separators=(",", ":"))
    row.hot_updated_at = taken_at
    row.taken_at = taken_at
    row.node_instance_id = instance_id or row.node_instance_id
    row.node_app_version = app_version or row.node_app_version
    row.shrink_flagged = bool(shrink_flagged)
    db.session.commit()


def write_cold(panel_id: int, cold: dict, *, fingerprint: str, taken_at: int) -> None:
    row = _current_or_new(panel_id)
    row.cold_state = json.dumps(cold, separators=(",", ":"))
    row.cold_fingerprint = fingerprint
    row.cold_updated_at = taken_at
    db.session.commit()


def archive_daily(panel_id: int, *, taken_at: int) -> None:
    current = read_current(panel_id)
    if current is None:
        return
    db.session.add(
        PanelStateMirror(
            panel_id=panel_id,
            kind=KIND_DAILY,
            taken_at=taken_at,
            hot_state=current.hot_state,
            hot_updated_at=current.hot_updated_at,
            cold_state=current.cold_state,
            cold_fingerprint=current.cold_fingerprint,
            cold_updated_at=current.cold_updated_at,
            node_app_version=current.node_app_version,
            node_instance_id=current.node_instance_id,
            shrink_flagged=current.shrink_flagged,
        )
    )
    db.session.commit()


def prune_archive(older_than_ms: int) -> int:
    removed = (
        PanelStateMirror.query.filter(
            PanelStateMirror.kind == KIND_DAILY, PanelStateMirror.taken_at < older_than_ms
        ).delete(synchronize_session=False)
        or 0
    )
    db.session.commit()
    return removed


def forget_mirror(panel_id: int) -> None:
    PanelStateMirror.query.filter_by(panel_id=panel_id).delete(synchronize_session=False)
    db.session.commit()


def load_state(row) -> tuple[dict, dict]:
    hot = json.loads(row.hot_state or "{}")
    cold = json.loads(row.cold_state or "{}")
    return hot, cold
