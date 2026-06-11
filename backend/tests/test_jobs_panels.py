import time
from unittest.mock import MagicMock, patch

from app.models import LinkedPanel
from app.jobs.panels import poll_linked_panels


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
        m.snapshot.return_value = snapshot if snapshot is not None else {"timestamp": int(time.time())}
    return m


def test_poll_skips_db_write_when_status_unchanged(app, db):
    panel = _make_panel(db, status="online", last_poll=12345)
    mock_redis = MagicMock()

    with (
        patch("app.jobs.panels.FederationClient", return_value=_client_mock()),
        patch("app.jobs.panels.get_redis", return_value=mock_redis),
        patch.object(db.session, "commit") as mock_commit,
    ):
        poll_linked_panels()

    assert mock_commit.call_count == 0
    keys = [c.args[0] for c in mock_redis.setex.call_args_list]
    assert f"panel:{panel.id}:last_poll" in keys
    assert f"panel:{panel.id}:status" in keys


def test_poll_commits_on_status_change(app, db):
    panel = _make_panel(db, status="offline", last_error="old failure")

    with (
        patch(
            "app.jobs.panels.FederationClient",
            return_value=_client_mock(snapshot={"timestamp": 1781200000}),
        ),
        patch("app.jobs.panels.get_redis", return_value=MagicMock()),
    ):
        poll_linked_panels()

    db.session.refresh(panel)
    assert panel.status == "online"
    assert panel.last_error is None
    assert panel.last_poll == 1781200000 * 1000


def test_poll_offline_commits_once_then_skips(app, db):
    panel = _make_panel(db, status="online")
    failing = _client_mock(exc=RuntimeError("conn refused"))

    with (
        patch("app.jobs.panels.FederationClient", return_value=failing),
        patch("app.jobs.panels.get_redis", return_value=MagicMock()),
    ):
        poll_linked_panels()
        db.session.refresh(panel)
        assert panel.status == "offline"
        assert "conn refused" in (panel.last_error or "")

        with patch.object(db.session, "commit") as mock_commit:
            poll_linked_panels()

    assert mock_commit.call_count == 0
