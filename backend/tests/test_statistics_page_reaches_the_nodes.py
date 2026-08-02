"""§8.5, revised: the Statistics page follows the data instead of being deleted from the product.

The wave-4d defect had two ends and neither was visible from the other. On the master the page
existed and the tables behind it were empty forever — nothing has written `traffic_snapshot` or
`domain_stat` into the shared Postgres since phase 3b. On a node the tables were full and there was
no page: `packages/node` routed four screens and Statistics was not among them, because phase 3e
classified it as master-only and that classification was inverted.

§8.5 (2026-07-28) resolved this by taking the page off the master. The customer overturned that on
2026-07-29 in favour of wave 4c-2's shape: the page comes back to the master **with a node picker**,
exactly like Routing, and the node gets it too. So there is no node-only path here — the surface is
symmetric, and what makes it correct is the scope, not the location.

Three separate places decided whether the page was reachable, and missing any one leaves it
unreachable while the other two look fixed: the sidebar's `WORKER_HIDDEN` filter, the route in
`packages/admin/App.tsx`, and the *absence* of a route in `packages/node/App.tsx`. That third one is
the one a reader of the first two cannot see.

The backend half is asserted in `test_statistics_over_federation.py`; this file is the bundle.
"""

import re

from tests.frontend_import_graph import PACKAGE_ROOTS

STATS_PAGE = PACKAGE_ROOTS["ui-core"] / "pages" / "Statistics.tsx"
SIDEBAR = PACKAGE_ROOTS["ui-core"] / "components" / "layout" / "Sidebar.tsx"
ADMIN_APP = PACKAGE_ROOTS["admin"] / "App.tsx"
NODE_APP = PACKAGE_ROOTS["node"] / "App.tsx"

SCOPED_ENDPOINTS = (
    "/stats/overview",
    "/stats/traffic",
    "/stats/domains",
    "/stats/domain-users",
    "/stats/users-ranking",
)

CACHE_KEYS = (
    "'stats-overview'",
    "'stats-traffic-all'",
    "'stats-traffic-user'",
    "'stats-traffic-inbound'",
    "'stats-domains'",
    "'stats-domain-users'",
    "'stats-users'",
)


def _page():
    return STATS_PAGE.read_text(encoding="utf-8")


def _flat():
    return re.sub(r"\s+", "", _page())


class TestThePageIsReachableOnBothRoles:
    def test_the_sidebar_no_longer_hides_it_from_a_node(self):
        body = SIDEBAR.read_text(encoding="utf-8")

        assert "'/statistics'" in body, "the menu entry itself must still exist"
        worker_hidden = body.split("WORKER_HIDDEN = new Set(", 1)[1].split(")", 1)[0]
        assert "/statistics" not in worker_hidden, (
            "the sidebar still filters /statistics out on a node — the one role that actually "
            "collects the traffic would have no way to reach the page"
        )

    def test_the_node_app_routes_it(self):
        """This is the assertion the other two cannot stand in for."""

        body = NODE_APP.read_text(encoding="utf-8")

        assert 'path="statistics" element={<Statistics />}' in body, (
            "packages/node has no /statistics route; the sidebar entry would lead nowhere and the "
            "node — the only role with traffic data — would still have no page"
        )
        assert "@ui/pages/Statistics" in body

    def test_the_admin_app_still_routes_it(self):
        body = ADMIN_APP.read_text(encoding="utf-8")

        assert 'path="statistics" element={<Statistics />}' in body
        assert "@ui/pages/Statistics" in body, "the admin app must import the shared page, not a copy of its own"

    def test_the_page_ships_from_ui_core_and_nowhere_else(self):
        """In `packages/admin` it reaches the master only; in `packages/node` the master loses it."""

        assert STATS_PAGE.is_file()
        assert not (PACKAGE_ROOTS["admin"] / "pages" / "Statistics.tsx").exists()
        assert not (PACKAGE_ROOTS["node"] / "pages" / "Statistics.tsx").exists()


class TestEveryRequestNamesTheNode:
    def test_the_page_picks_a_panel(self):
        body = _page()

        assert "useLinkedPanels" in body, "the picker needs the list of nodes"
        assert "isWorker ? null : panelId" in body, "a node reports on itself and must send no panel_id"

    def test_every_stats_request_goes_through_the_scoping_helper(self):
        """Whitespace-insensitive on purpose — prettier reflows these calls across lines."""

        body = _flat()

        assert "panel_id=${panelId}" in body
        for endpoint in SCOPED_ENDPOINTS:
            assert f"statsScopedPath(`{endpoint}" in body, (
                f"{endpoint} is requested unscoped; on a master that asks a role which has no data and now answers 501"
            )

    def test_no_bare_request_to_a_stats_endpoint_survives(self):
        """The failure mode is a leftover call, not a missing feature."""

        body = _flat()

        for endpoint in SCOPED_ENDPOINTS:
            assert f"api.get(`{endpoint}" not in body, f"{endpoint} is still called without a scope"

    def test_cache_keys_carry_the_panel_too(self):
        """Miss the key and React Query serves node A's traffic under node B's picker."""

        body = _page()

        for key in CACHE_KEYS:
            head = body.split(f"queryKey: [{key},", 1)
            assert len(head) == 2, f"{key} is not a scoped query key any more"
            assert "panelId]" in head[1].split("\n", 1)[0], (
                f"{key} is cached without the panel id; switching nodes would show the previous node's numbers"
            )

    def test_the_master_waits_for_a_scope_before_asking_anything(self):
        """Without this, the first render fires seven unscoped requests at a role that has no data."""

        body = _page()

        assert "const scopeResolved = isWorker || panelId != null;" in body
        assert body.count("enabled: scopeResolved") == 7, (
            "all seven queries must wait for the scope; one that does not is one 501 per page load"
        )


class TestADeadNodeIsSaidOutLoud:
    """Customer decision carried over from 4c-2: an explicit error, never zeroes.

    Zeroes read as "this node has no traffic" — which is exactly the lie this wave removes from the
    master, so re-introducing it as a failure mode would be self-defeating.
    """

    def test_the_page_has_an_error_state(self):
        body = _page()

        assert "function StatsNodeUnreachable" in body
        assert body.count("<StatsNodeUnreachable error=") == 4, (
            "all four tabs must fail loudly; one that still charts zeroes is the defect"
        )

    def test_every_tab_hides_its_content_when_its_query_failed(self):
        body = _page()

        for guard in (
            "{tab === 'overview' && !overviewError && (",
            "{tab === 'users' && !usersError && (",
            "{tab === 'inbounds' && !overviewError && (",
            "{tab === 'sites' && !domainsError && (",
        ):
            assert guard in body, f"missing: {guard} — the tab would render zeroes next to the error"

    def test_a_failed_read_is_not_retried_into_a_hang(self):
        body = _page()

        assert body.count("retry: false,") == 7

    def test_the_nodes_own_message_is_what_is_shown(self):
        body = _page()

        assert "function statsErrorMessage" in body
        assert "response?.data?.error" in body

    def test_a_master_with_no_nodes_says_so_instead_of_charting_nothing(self):
        body = _page()

        assert "No nodes to report on" in body
        assert "selectablePanels.length === 0" in body
