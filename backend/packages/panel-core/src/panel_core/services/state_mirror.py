import json

from panel_core.extensions import db
from panel_core.models import LinkedPanel, PanelStateMirror

KIND_CURRENT = "current"
KIND_DAILY = "daily"
COHERENCE_MARKER = "_coherent_snapshot"


def read_current(panel_id: int) -> PanelStateMirror | None:
    return PanelStateMirror.query.filter_by(panel_id=panel_id, kind=KIND_CURRENT).first()


def _current_or_new(panel_id: int) -> PanelStateMirror:
    row = read_current(panel_id)
    if row is None:
        row = PanelStateMirror(panel_id=panel_id, kind=KIND_CURRENT, taken_at=0)
        db.session.add(row)
    return row


def write_hot(
    panel_id: int,
    hot: dict,
    *,
    taken_at: int,
    instance_id: str,
    app_version: str,
    shrink_flagged: bool,
    commit: bool = True,
) -> bool:
    lock_panel(panel_id)
    row = _current_or_new(panel_id)
    if row.node_instance_id == instance_id and (row.hot_updated_at or 0) > taken_at:
        if commit:
            db.session.commit()
        return False
    if row.node_instance_id and row.node_instance_id != instance_id:
        row.cold_state = ""
        row.cold_updated_at = None
        row.cold_fingerprint = None
    row.hot_state = json.dumps(hot, separators=(",", ":"))
    row.hot_updated_at = taken_at
    row.taken_at = taken_at
    row.node_instance_id = instance_id or row.node_instance_id
    row.node_app_version = app_version or row.node_app_version
    row.shrink_flagged = bool(shrink_flagged)
    if commit:
        db.session.commit()
    return True


def write_cold(panel_id: int, cold: dict, *, fingerprint: str, taken_at: int, commit: bool = True) -> bool:
    lock_panel(panel_id)
    row = _current_or_new(panel_id)
    if (row.cold_updated_at or 0) > taken_at:
        if commit:
            db.session.commit()
        return False
    row.cold_state = json.dumps(cold, separators=(",", ":"))
    row.cold_fingerprint = fingerprint
    row.cold_updated_at = taken_at
    if commit:
        db.session.commit()
    return True


def lock_panel(panel_id):
    count = LinkedPanel.query.filter_by(id=panel_id).update(
        {"poll_applied_generation": LinkedPanel.poll_applied_generation},
        synchronize_session=False,
    )
    if not count:
        return None
    return LinkedPanel.query.filter_by(id=panel_id).populate_existing().first()


def validate_state(hot, cold):
    if not isinstance(hot, dict) or not isinstance(hot.get("inbounds"), list):
        raise ValueError("state has no complete hot snapshot")
    if not isinstance(cold, dict) or any(
        not isinstance(cold.get(key), list)
        for key in (
            "outbounds",
            "routing_profiles",
            "balancers",
            "settings",
            "receipts",
            "notification_logs",
            "events",
            "entitlements",
            "account_access",
        )
    ):
        raise ValueError("state has no complete cold snapshot")
    if not isinstance(cold.get("identity"), dict):
        raise ValueError("state has no node identity")
    for event in cold["events"]:
        if not isinstance(event, dict) or not event.get("source") or not isinstance(event.get("origin_event_id"), int):
            raise ValueError("state has an event without a durable origin")


def write_full(
    panel_id, hot, cold, *, taken_at, fingerprint, instance_id, app_version="", shrink_flagged=False, commit=True
):
    validate_state(hot, cold)
    lock_panel(panel_id)
    row = _current_or_new(panel_id)
    if row.node_instance_id == instance_id and max(row.hot_updated_at or 0, row.cold_updated_at or 0) > taken_at:
        if commit:
            db.session.commit()
        return False
    write_hot(
        panel_id,
        hot,
        taken_at=taken_at,
        instance_id=instance_id,
        app_version=app_version,
        shrink_flagged=shrink_flagged,
        commit=False,
    )
    row.cold_updated_at = None
    write_cold(panel_id, {**cold, COHERENCE_MARKER: 1}, fingerprint=fingerprint, taken_at=taken_at, commit=False)
    if commit:
        db.session.commit()
    return True


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
    cold.pop(COHERENCE_MARKER, None)
    return hot, cold


def state_is_coherent(row) -> bool:
    if row is None or not row.cold_state:
        return False
    try:
        cold = json.loads(row.cold_state)
        if cold.get(COHERENCE_MARKER) != 1:
            return False
        validate_state(json.loads(row.hot_state), cold)
        return True
    except (ValueError, AttributeError):
        return False
