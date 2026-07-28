import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from panel_core.models import LinkedPanel
from panel_core.services.panel_proxy import (
    REFRESH_CHANNEL,
    FederationClient,
    _get_panel_or_raise,
    fetch_panel_snapshot_live,
    get_panel_snapshot,
    proxy_bulk_set_flow,
    proxy_create_user,
    proxy_delete_user,
    proxy_provision,
)


def _make_panel(db, *, name="test-panel", status="online", enable=True):
    now = int(time.time())
    panel = LinkedPanel(
        name=name,
        url="https://child.example.com",
        federation_token="fed-token-secret",
        status=status,
        enable=enable,
        created_at=now,
    )
    db.session.add(panel)
    db.session.commit()
    return panel


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


class TestFederationClient:
    def setup_method(self):
        self.client = FederationClient(
            url="https://child.example.com/",
            federation_token="tok-abc",
        )

    def test_token_header_is_set(self):
        assert self.client._session.headers["X-Federation-Token"] == "tok-abc"

    def test_base_url_strips_trailing_slash(self):
        assert self.client.base_url == "https://child.example.com"

    def test_snapshot_calls_correct_url_with_split_timeout(self):

        with patch.object(self.client._session, "get", return_value=_mock_response({"inbounds": []})) as mock_get:
            result = self.client.snapshot()
        mock_get.assert_called_once_with(
            "https://child.example.com/api/federation/snapshot",
            timeout=(2, 5),
        )
        assert result == {"inbounds": []}

    def test_snapshot_raises_on_http_error(self):
        bad_resp = MagicMock()
        bad_resp.raise_for_status.side_effect = requests.HTTPError("503")
        with patch.object(self.client._session, "get", return_value=bad_resp):
            with pytest.raises(requests.HTTPError):
                self.client.snapshot()

    def test_create_inbound_posts_json(self):
        payload = {"tag": "vless-in", "port": 443}
        with patch.object(self.client._session, "post", return_value=_mock_response({"ok": True})) as mock_post:
            result = self.client.create_inbound(payload)
        mock_post.assert_called_once_with(
            "https://child.example.com/api/inbounds",
            json=payload,
            timeout=8,
        )
        assert result == {"ok": True}

    def test_update_inbound_puts_to_tag_url(self):
        with patch.object(self.client._session, "put", return_value=_mock_response({"ok": True})) as mock_put:
            self.client.update_inbound("vless-in", {"port": 444})
        mock_put.assert_called_once_with(
            "https://child.example.com/api/inbounds/vless-in",
            json={"port": 444},
            timeout=8,
        )

    def test_delete_inbound_sends_delete(self):
        with patch.object(self.client._session, "delete", return_value=_mock_response({"ok": True})) as mock_delete:
            self.client.delete_inbound("vless-in")
        mock_delete.assert_called_once_with(
            "https://child.example.com/api/inbounds/vless-in",
            timeout=8,
        )

    def test_create_user_posts_to_inbound_users(self):
        user_data = {"email": "alice", "id": "uuid-1"}
        with patch.object(self.client._session, "post", return_value=_mock_response({"ok": True})) as mock_post:
            self.client.create_user("vless-in", user_data)
        mock_post.assert_called_once_with(
            "https://child.example.com/api/inbounds/vless-in/users",
            json=user_data,
            timeout=8,
        )

    def test_update_user_puts_to_inbound_users(self):
        user_data = {"email": "alice", "limit_bytes": 1000}
        with patch.object(self.client._session, "put", return_value=_mock_response({"ok": True})) as mock_put:
            self.client.update_user("vless-in", user_data)
        mock_put.assert_called_once_with(
            "https://child.example.com/api/inbounds/vless-in/users",
            json=user_data,
            timeout=8,
        )

    def test_delete_user_passes_email_as_query_param(self):
        with patch.object(self.client._session, "delete", return_value=_mock_response({"ok": True})) as mock_delete:
            self.client.delete_user("vless-in", "alice@panel")
        mock_delete.assert_called_once_with(
            "https://child.example.com/api/inbounds/vless-in/users",
            params={"email": "alice@panel"},
            timeout=8,
        )

    def test_provision_posts_telegram_id_and_inbound_tag(self):
        params = {"expiry_time": 9999999999, "limit_bytes": 0}
        with patch.object(self.client._session, "post", return_value=_mock_response({"ok": True})) as mock_post:
            self.client.provision(12345, "vless-in", params)
        mock_post.assert_called_once_with(
            "https://child.example.com/api/federation/provision",
            json={"telegram_id": 12345, "inbound_tag": "vless-in", **params},
            timeout=8,
        )

    def test_bulk_set_flow_posts_users_and_flow(self):
        users = [{"tag": "vless-in", "email": "a"}]
        with patch.object(self.client._session, "post", return_value=_mock_response({"updated": 1})) as mock_post:
            result = self.client.bulk_set_flow(users, "xtls-rprx-vision")
        mock_post.assert_called_once_with(
            "https://child.example.com/api/users/bulk-set-flow",
            json={"users": users, "flow": "xtls-rprx-vision"},
            timeout=30,
        )
        assert result == {"updated": 1}


class TestGetPanelOrRaise:
    def test_returns_panel_when_online(self, app, db):
        panel = _make_panel(db, status="online")
        result = _get_panel_or_raise(panel.id)
        assert result.id == panel.id

    def test_raises_when_not_found(self, app, db):
        with pytest.raises(ValueError, match="not found"):
            _get_panel_or_raise(99999)

    def test_raises_when_disabled(self, app, db):
        panel = _make_panel(db, name="disabled", status="online", enable=False)
        with pytest.raises(ValueError, match="disabled"):
            _get_panel_or_raise(panel.id)

    def test_a_panel_recorded_offline_is_still_attempted(self, app, db):

        panel = _make_panel(db, name="offline-panel", status="offline")
        assert _get_panel_or_raise(panel.id).id == panel.id

    def test_unknown_status_does_not_raise(self, app, db):

        panel = _make_panel(db, name="unknown-panel", status="unknown")
        result = _get_panel_or_raise(panel.id)
        assert result.id == panel.id


class TestGetPanelSnapshot:
    def test_returns_none_when_redis_unavailable(self, app):
        with patch("panel_core.services.panel_proxy.get_shared_redis", return_value=None):
            assert get_panel_snapshot(1) is None

    def test_returns_none_on_cache_miss(self, app):
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch("panel_core.services.panel_proxy.get_shared_redis", return_value=mock_redis):
            assert get_panel_snapshot(42) is None
        mock_redis.get.assert_called_once_with("panel:42:snapshot")

    def test_returns_parsed_json_on_hit(self, app):
        import json as _json

        data = {"inbounds": [{"tag": "vless-in"}]}
        mock_redis = MagicMock()
        mock_redis.get.return_value = _json.dumps(data).encode()
        with patch("panel_core.services.panel_proxy.get_shared_redis", return_value=mock_redis):
            result = get_panel_snapshot(7)
        assert result == data

    def test_returns_none_on_redis_error(self, app):
        mock_redis = MagicMock()
        mock_redis.get.side_effect = Exception("connection refused")
        with patch("panel_core.services.panel_proxy.get_shared_redis", return_value=mock_redis):
            assert get_panel_snapshot(1) is None


class TestProxyCreateUser:
    def test_a_panel_recorded_offline_is_still_attempted(self, app, db):

        panel = _make_panel(db, name="offline-for-create", status="offline")
        mock_client = MagicMock()
        mock_client.create_user.return_value = {"ok": True}
        with patch("panel_core.services.panel_proxy.FederationClient", return_value=mock_client):
            assert proxy_create_user(panel.id, "vless-in", {"email": "bob"}) == {"ok": True}

    def test_raises_on_missing_panel(self, app, db):
        with pytest.raises(ValueError, match="not found"):
            proxy_create_user(88888, "vless-in", {"email": "bob"})

    def test_calls_client_and_returns_result(self, app, db):
        panel = _make_panel(db, name="proxy-create-user")
        user_data = {"email": "carol", "id": "uuid-99"}
        expected = {"created": True}

        with patch("panel_core.services.panel_proxy.FederationClient") as MockClient:
            instance = MockClient.return_value
            instance.create_user.return_value = expected

            instance.snapshot.return_value = {}

            result = proxy_create_user(panel.id, "vless-in", user_data)

        MockClient.assert_called_with(panel.url, panel.federation_token)
        instance.create_user.assert_called_once_with("vless-in", user_data)
        assert result == expected


class TestProxyDeleteUser:
    def test_a_panel_recorded_offline_is_still_attempted(self, app, db):
        panel = _make_panel(db, name="offline-for-delete", status="offline")
        mock_client = MagicMock()
        mock_client.delete_user.return_value = {"deleted": True}
        with patch("panel_core.services.panel_proxy.FederationClient", return_value=mock_client):
            assert proxy_delete_user(panel.id, "vless-in", "alice@panel") == {"deleted": True}

    def test_calls_client_delete_user(self, app, db):
        panel = _make_panel(db, name="proxy-delete-user")
        expected = {"deleted": True}

        with patch("panel_core.services.panel_proxy.FederationClient") as MockClient:
            instance = MockClient.return_value
            instance.delete_user.return_value = expected
            instance.snapshot.return_value = {}

            result = proxy_delete_user(panel.id, "vless-in", "alice@panel")

        instance.delete_user.assert_called_once_with("vless-in", "alice@panel")
        assert result == expected


class TestProxyBulkSetFlow:
    def test_a_panel_recorded_offline_is_still_attempted(self, app, db):
        panel = _make_panel(db, name="offline-for-flow", status="offline")
        mock_client = MagicMock()
        mock_client.bulk_set_flow.return_value = {"status": "ok"}
        with patch("panel_core.services.panel_proxy.FederationClient", return_value=mock_client):
            result = proxy_bulk_set_flow(panel.id, [{"tag": "vless-in", "email": "a"}], "xtls-rprx-vision")
        assert result == {"status": "ok"}

    def test_calls_client_bulk_set_flow(self, app, db):
        panel = _make_panel(db, name="proxy-set-flow")
        users = [{"tag": "vless-in", "email": "a"}]
        expected = {"status": "ok", "updated": 1, "skipped": 0}

        with patch("panel_core.services.panel_proxy.FederationClient") as MockClient:
            instance = MockClient.return_value
            instance.bulk_set_flow.return_value = expected
            instance.snapshot.return_value = {}

            result = proxy_bulk_set_flow(panel.id, users, "xtls-rprx-vision")

        instance.bulk_set_flow.assert_called_once_with(users, "xtls-rprx-vision")
        assert result == expected


class TestFetchPanelSnapshotLive:
    def test_returns_snapshot(self, app, db):
        panel = _make_panel(db, name="snapshot-live")
        expected = {"inbounds": [{"tag": "x", "clients": []}]}

        with patch("panel_core.services.panel_proxy.FederationClient") as MockClient:
            instance = MockClient.return_value
            instance.snapshot.return_value = expected

            result = fetch_panel_snapshot_live(panel.id)

        MockClient.assert_called_with(panel.url, panel.federation_token)
        assert result == expected

    def test_a_panel_recorded_offline_is_still_attempted(self, app, db):
        panel = _make_panel(db, name="snapshot-live-offline", status="offline")
        with patch("panel_core.services.panel_proxy.FederationClient") as MockClient:
            MockClient.return_value.snapshot.return_value = {"inbounds": []}
            assert fetch_panel_snapshot_live(panel.id) == {"inbounds": []}


class TestNudgePanelRefresh:
    def test_a_proxied_operation_publishes_a_nudge_instead_of_polling_the_node(self, app, db):

        panel = _make_panel(db, name="nudge-instead-of-poll")
        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_client.create_user.return_value = {"ok": True}

        with (
            patch("panel_core.services.panel_proxy.FederationClient", return_value=mock_client),
            patch("panel_core.services.panel_proxy.get_shared_redis", return_value=mock_redis),
            patch.object(db.session, "commit") as mock_commit,
        ):
            proxy_create_user(panel.id, "vless-in", {"email": "bob"})

        mock_redis.publish.assert_called_once_with(REFRESH_CHANNEL, str(panel.id))
        assert mock_client.snapshot.call_count == 0, (
            "the proxied operation went to the node for a fresh snapshot. That second HTTP call sat inside the "
            "payment confirmation path with a timeout of up to 7 s; wave 2 replaced it with a publish so the "
            "cron service — the only snapshot writer — picks the panel up out of band."
        )
        assert mock_commit.call_count == 0

    def test_the_nudge_never_deletes_the_snapshot(self, app, db):

        panel = _make_panel(db, name="nudge-never-deletes")
        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_client.create_user.return_value = {"ok": True}

        with (
            patch("panel_core.services.panel_proxy.FederationClient", return_value=mock_client),
            patch("panel_core.services.panel_proxy.get_shared_redis", return_value=mock_redis),
        ):
            proxy_create_user(panel.id, "vless-in", {"email": "bob"})

        assert mock_redis.delete.call_count == 0, (
            "the nudge deleted the snapshot key. For the sub host a MISSING key does not mean 'stale', it "
            "means 'this panel has no remote clients at all' — it skips the panel entirely. A user who has "
            "just paid would open the link and see a subscription without any node servers in it. A stale "
            "snapshot is strictly better than an absent one here."
        )
        assert mock_redis.setex.call_count == 0

    def test_a_failed_nudge_does_not_break_the_operation(self, app, db):

        panel = _make_panel(db, name="nudge-fail")
        mock_redis = MagicMock()
        mock_redis.publish.side_effect = requests.ConnectionError("boom")
        mock_client = MagicMock()
        mock_client.provision.return_value = {"client": {"id": "x"}}

        with (
            patch("panel_core.services.panel_proxy.FederationClient", return_value=mock_client),
            patch("panel_core.services.panel_proxy.get_shared_redis", return_value=mock_redis),
        ):
            result = proxy_provision(panel.id, 42, "vless-in", {"expiry_ms": 1})

        assert result == {"client": {"id": "x"}}

    def test_a_lost_nudge_needs_no_journal_unlike_the_event_bus(self, app, db):

        panel = _make_panel(db, name="nudge-no-journal")
        with (
            patch("panel_core.services.panel_proxy.FederationClient", return_value=MagicMock()),
            patch("panel_core.services.panel_proxy.get_shared_redis", return_value=None),
        ):
            assert proxy_create_user(panel.id, "vless-in", {"email": "bob"}) is not None
