"""§8.9 step 4: the Routing page comes back to the master and learns which node it is editing.

Three separate places hid the page from a role with no local Xray, and missing any one of them
leaves it unreachable while the other two look fixed: the sidebar filtered `/routing` out of the
menu, and **both** `App.tsx` files redirected the route to `/`. A guard that only checked the
sidebar would pass against a build where the link exists and the route bounces.

The page stays in `ui-core` rather than moving to `packages/admin` (customer decision): it is the
one screen that is meaningful on both roles, and moving it would take it away from the node, where
it is the *only* place these settings can be edited. The cost is that a change here rebuilds both
frontend images — remember it at version-bump time, do not "fix" it by moving the file.

What makes the page correct on a master is not the route but the scope: every request carries
`?panel_id=`, and every `queryKey` carries the same id. Miss the key and React Query serves node
A's outbounds under node B's tab from cache — the reads would look right for one node and be
silently wrong for the rest.

`/outbounds/health` is deliberately **not** scoped. A reachability probe run from the master
measures the master's route to the endpoint, not the node's, so the customer's decision (4c-1,
item 5) is that the column simply does not exist off the node.
"""

from tests.frontend_import_graph import PACKAGE_ROOTS

ROUTING_PAGE = PACKAGE_ROOTS["ui-core"] / "pages" / "Routing.tsx"
SIDEBAR = PACKAGE_ROOTS["ui-core"] / "components" / "layout" / "Sidebar.tsx"
ADMIN_APP = PACKAGE_ROOTS["admin"] / "App.tsx"
NODE_APP = PACKAGE_ROOTS["node"] / "App.tsx"

SCOPED_ENDPOINTS = (
    "'/routing-profiles'",
    "'/outbounds'",
    "'/balancers'",
)


def _routing():
    return ROUTING_PAGE.read_text(encoding="utf-8")


class TestThePageIsReachableOnAMaster:
    def test_the_sidebar_no_longer_hides_it(self):
        body = SIDEBAR.read_text(encoding="utf-8")

        assert "LOCAL_XRAY_ONLY" not in body, (
            "the sidebar filtered /routing out on any role without a local Xray, which is every master"
        )
        assert "'/routing'" in body, "the menu entry itself must still exist"

    def test_neither_app_redirects_the_route_away(self):
        for path in (ADMIN_APP, NODE_APP):
            body = path.read_text(encoding="utf-8")
            assert 'path="routing" element={<Routing />}' in body, (
                f"{path.name} still gates the route; the sidebar entry would lead to a redirect"
            )
            assert "hasLocalXray" not in body, f"{path.name} keeps the gate that made the page master-less"

    def test_the_page_still_ships_from_ui_core(self):
        """Moving it to `packages/admin` would fix the master and break the node."""

        assert ROUTING_PAGE.is_file()
        assert not (PACKAGE_ROOTS["admin"] / "pages" / "Routing.tsx").exists()


class TestEveryRequestNamesTheNode:
    def test_the_page_picks_a_panel_and_shares_it_downwards(self):
        body = _routing()

        assert "useLinkedPanels" in body, "the picker needs the list of nodes"
        assert "PanelScopeContext" in body, "six components need the id; prop-drilling through modals loses it"
        assert "isWorker ? null : panelId" in body, "a node edits itself and must send no panel_id"

    def test_requests_are_scoped_through_one_helper(self):
        body = _routing()

        assert "panel_id=${panelId}" in body
        for endpoint in SCOPED_ENDPOINTS:
            assert f"scopedPath({endpoint}, panelId)" in body, (
                f"{endpoint} is requested unscoped; on a master that asks the wrong panel"
            )

    def test_no_bare_request_to_a_scoped_endpoint_survives(self):
        """The failure mode this catches is a leftover call, not a missing feature."""

        body = _routing()

        for endpoint in SCOPED_ENDPOINTS:
            assert f"api.get<Outbound[]>({endpoint})" not in body
            assert f"api.post({endpoint}," not in body

    def test_cache_keys_carry_the_panel_too(self):
        body = _routing()

        for key in ("'routing-profiles'", "'outbounds'", "'balancers'"):
            assert f"queryKey: [{key}, panelId]" in body, (
                f"{key} is cached without the panel id; switching nodes would show the previous node's rows"
            )
            assert f"queryKey: [{key}] " not in body and f"queryKey: [{key}]," not in body

    def test_the_rule_editor_offers_only_the_selected_nodes_inbounds(self):
        """`GET /inbounds` on a master merges every panel's inbounds into one list.

        Unfiltered, an admin would bind node A's rule to node B's inbound tag, and the node would
        reject it at validation time with a message about a tag that exists — somewhere else.
        """

        body = _routing()

        assert "`/inbounds?panel=${panelId}`" in body
        assert "'/inbounds?panel=local'" in body


class TestHealthStaysOnTheNode:
    def test_the_probe_is_not_scoped_to_a_panel(self):
        body = _routing()

        assert "scopedPath('/outbounds/health'" not in body
        assert "api.get<OutboundHealth[]>('/outbounds/health')" in body

    def test_the_probe_is_not_even_issued_when_a_node_is_selected(self):
        body = _routing()

        assert "enabled: !panelId," in body, "a master would otherwise call an endpoint that answers 501"

    def test_the_column_disappears_with_it(self):
        body = _routing()

        assert "const healthShown = !panelId;" in body
        assert "{healthShown && (" in body, "leaving the chips rendered would show every outbound as 'unknown'"


class TestADeadNodeIsSaidOutLoud:
    """Customer decision: an explicit error, never an empty list.

    An empty list reads as "this node has no outbounds" and invites the admin to create them again
    on top of the ones already there — the same shape of lie the backend half of this wave removes.
    """

    def test_the_page_has_an_error_state(self):
        body = _routing()

        assert "function PanelUnreachable" in body
        assert body.count("<PanelUnreachable error={error} onRetry={() => refetch()} />") == 3, (
            "all three tabs must fail loudly; one that still renders an empty list is the defect"
        )

    def test_a_failed_read_is_not_retried_into_a_hang(self):
        body = _routing()

        assert body.count("retry: false,") >= 3

    def test_the_nodes_own_message_is_what_is_shown(self):
        body = _routing()

        assert "function remoteErrorMessage" in body
        assert "response?.data?.error" in body
