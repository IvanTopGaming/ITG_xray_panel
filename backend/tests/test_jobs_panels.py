import time
from unittest.mock import MagicMock, patch

from panel_core.models import LinkedPanel
from panel_core.jobs.panels import poll_linked_panels


def _make_panel(db, *, name="p1", status="online", last_poll=None, last_error=None):
    panel = LinkedPanel(
        name=name,
        url="https://child.example.com",
        federation_token="tok",
        status=status,
        enable=True,
        created_at=int(time.time()),
        last_poll=last_poll,
        last_error=last_error,
    )
    db.session.add(panel)
    db.session.commit()
    return panel


def _client_mock(snapshot=None, exc=None):
    m = MagicMock()
    if exc is not None:
        m.snapshot.side_effect = exc
    else:
        m.snapshot.return_value = {
            "timestamp": int(time.time()),
            "inbounds": [],
            "instance_id": "node",
            **(snapshot or {}),
        }
    return m


def test_poll_persists_fence_when_status_unchanged(app, db):
    panel = _make_panel(db, status="online", last_poll=12345)
    mock_redis = MagicMock()

    with (
        patch("panel_core.jobs.panels.FederationClient", return_value=_client_mock()),
        patch("panel_core.services.panel_proxy.get_shared_redis", return_value=mock_redis),
    ):
        poll_linked_panels()

    db.session.refresh(panel)
    assert panel.status == "online"
    assert panel.poll_generation == panel.poll_applied_generation == 1
    keys = list(mock_redis.eval.call_args.args[2:8])
    assert f"panel:{panel.id}:last_poll" in keys
    assert f"panel:{panel.id}:status" in keys


def test_poll_commits_on_status_change(app, db):
    panel = _make_panel(db, status="offline", last_error="old failure")

    with (
        patch(
            "panel_core.jobs.panels.FederationClient",
            return_value=_client_mock(snapshot={"timestamp": 1781200000}),
        ),
        patch("panel_core.services.panel_proxy.get_shared_redis", return_value=MagicMock()),
    ):
        poll_linked_panels()

    db.session.refresh(panel)
    assert panel.status == "online"
    assert panel.last_error is None
    assert panel.last_poll == 1781200000 * 1000


def test_repeated_offline_poll_advances_fence_without_changing_failure(app, db):
    panel = _make_panel(db, status="online")
    failing = _client_mock(exc=RuntimeError("conn refused"))

    with (
        patch("panel_core.jobs.panels.FederationClient", return_value=failing),
        patch("panel_core.services.panel_proxy.get_shared_redis", return_value=MagicMock()),
    ):
        poll_linked_panels()
        db.session.refresh(panel)
        assert panel.status == "offline"
        assert "conn refused" in (panel.last_error or "")

        first_error = panel.last_error
        poll_linked_panels()

    db.session.refresh(panel)
    assert panel.status == "offline"
    assert panel.last_error == first_error
    assert panel.poll_generation == panel.poll_applied_generation == 2
