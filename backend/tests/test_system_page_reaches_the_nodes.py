"""§66, the bundle half: the master's System page can address a node's Xray.

The backend half is in `test_xray_control_over_federation.py`. This file exists because the bundle
is where waves of this shape actually get left half-finished: the capability is decided by a
`hasLocalXray` gate repeated in several places, and `hasLocalXray` is **false on the master**, so
every one of them has to come off. Missing one leaves a screen that looks fixed and is not.

Before this wave `System.tsx` gated on `hasLocalXray` in seven places -- the Core tab entry, the
`enabled:` of the settings query, the log panel, the grid layout, the Core tab body, the three
maintenance buttons, the two confirmation modals and the config modal -- and `Dashboard.tsx` in two
more, the Route button and its modal. Wave 4c-2 had three such places and every one had to be found
separately; §78 is the case where the *missed* one would have turned the wave into a regression.

Two of the nine deliberately stay:

- **the log panel and the grid layout it lives in.** `GET /api/logs` is a stream, not a response,
  and both `FederationClient` methods end in `.json()`; proxying it is a different piece of work
  (customer decision, wave 5c scope). The panel stays node-only, and it stays honest.
- **the Maintenance backup card.** That branch is wave 4c-1's: the master's own database is backed
  up by `pg-backup`, and a node's from its card on the Panels page.

The other load-bearing property here is the **scope of the picker** (customer decision): it covers
the Xray half of the page and nothing else. Security is the master's own admin password and About
is this panel's versions; neither changes when another node is selected, and making them look like
they do would be a new lie in place of the one this wave removes.
"""

import re

from tests.frontend_import_graph import PACKAGE_ROOTS

SYSTEM_PAGE = PACKAGE_ROOTS["ui-core"] / "pages" / "System.tsx"
DASHBOARD = PACKAGE_ROOTS["ui-core"] / "pages" / "Dashboard.tsx"

SCOPED_CALLS = (
    "api.get(`/system/settings${xrayScope}`)",
    "api.put(`/system/settings${xrayScope}`",
    "api.post(`/system/update-geo${xrayScope}`)",
    "api.post(`/restart${xrayScope}`)",
    "api.get(`/config${xrayScope}`)",
)

UNSCOPED_CALLS = (
    "api.get('/system/settings')",
    "api.put('/system/settings'",
    "api.post('/system/update-geo')",
    "api.post('/restart')",
    "api.get('/config')",
)


def _system():
    return SYSTEM_PAGE.read_text(encoding="utf-8")


def _dashboard():
    return DASHBOARD.read_text(encoding="utf-8")


def _flat(body):
    return re.sub(r"\s+", "", body)


class TestTheCoreTabExistsOnTheMaster:
    def test_the_tab_entry_is_not_gated_on_a_local_xray(self):
        """Gate one. With it in place the master has no Core tab and the rest is unreachable."""

        tabs = _system().split("const SETTINGS_TABS", 1)[1].split("];", 1)[0]

        assert "{ id: 'core', label: 'Core' }" in tabs, (
            "the Core tab is still conditional; on a master `hasLocalXray` is false, so the whole "
            "Xray surface would stay invisible no matter what the backend accepts"
        )

    def test_the_tab_body_is_not_gated_on_a_local_xray(self):
        """Gate two. The tab renders and its contents do not -- an empty card."""

        body = _flat(_system())

        assert "{activeTab==='core'&&(" in body
        assert "hasLocalXray&&activeTab==='core'" not in body

    def test_the_settings_query_waits_for_a_scope_instead_of_a_local_xray(self):
        """Gate three, and the quiet one: `enabled: hasLocalXray` means the master never asks."""

        body = _system()

        assert "enabled: xrayScopeResolved" in body
        assert "enabled: hasLocalXray" not in body, (
            "a query still waits on hasLocalXray; on the master it would never fire and the form "
            "would sit on its defaults while the backend was perfectly able to answer"
        )

    def test_the_maintenance_buttons_are_not_gated_on_a_local_xray(self):
        """Gate four: Update GeoIP, View Configuration and Restart Core share one condition."""

        body = _flat(_system())

        assert "onClick={()=>setConfirmGeoUpdate(true)}" in body
        assert "onClick={fetchConfig}" in body
        assert "onClick={()=>setConfirmRestart(true)}" in body
        assert body.count("{xrayScopeResolved&&(") == 3, (
            "three blocks must hang off the scope, not off a local Xray: the maintenance buttons, "
            "the restart/geo confirmations and the config modal. A count keeps this test failing "
            "for its own reason instead of leaning on the modal count below."
        )

    def test_the_modals_are_not_gated_on_a_local_xray(self):
        """Gate five and six. A button whose modal never renders does nothing and says nothing."""

        body = _flat(_system())

        assert "isOpen={confirmRestart}" in body
        assert "isOpen={confirmGeoUpdate}" in body
        assert "isOpen={configModal}" in body
        assert body.count("{hasLocalXray&&(") == 1, (
            "exactly one `hasLocalXray` block may remain: the log panel, which stays node-only "
            "because GET /api/logs is a stream and was left out of this wave on purpose"
        )


class TestEveryRequestNamesTheNode:
    def test_the_page_picks_a_node(self):
        body = _system()

        assert "useLinkedPanels(!isWorker)" in body, "the picker needs the list of nodes"
        assert "const xrayScope = hasLocalXray ? '' : panelId != null ? `?panel_id=${panelId}` : '';" in body, (
            "a node addresses its own Xray and must send no panel_id; a master must send one"
        )

    def test_every_xray_request_carries_the_scope(self):
        body = _flat(_system())

        for call in SCOPED_CALLS:
            assert _flat(call) in body, f"{call} is missing; that endpoint would be requested unscoped"

    def test_no_unscoped_call_survives(self):
        """The failure mode is a leftover call, not a missing feature (§78)."""

        body = _flat(_system())

        for call in UNSCOPED_CALLS:
            assert _flat(call) not in body, f"{call} still asks the master about an Xray it does not run"

    def test_the_settings_cache_key_carries_the_node(self):
        """Miss the key and React Query shows node A's log level under node B's picker."""

        body = _system()

        assert "queryKey: ['system-settings', panelId]" in body


class TestTheScreenNeverGoesQuietlyEmpty:
    def test_an_unreachable_node_is_an_error_with_a_retry(self):
        body = _system()

        assert "XrayNodeUnreachable" in body
        assert "Retry" in body
        assert "onRetry={() => refetchSettings()}" in body
        assert "retry: false" in body, (
            "without this the query retries silently and the admin watches a spinner instead of "
            "reading what the node said"
        )

    def test_no_linked_nodes_says_so_rather_than_showing_an_empty_form(self):
        body = _system()

        assert "noNodesToManage" in body
        assert "Panels page" in body


class TestThePickerScopesTheXrayHalfOnly:
    def test_it_covers_core_and_maintenance(self):
        body = _system()

        assert "const NODE_SCOPED_TABS: SettingsTab[] = ['core', 'maintenance'];" in body, (
            "customer decision: the picker scopes the Xray half. Security is this panel's own admin "
            "password and About is its own versions -- a picker over those would be a new lie"
        )
        assert "const showNodePicker = !isWorker && NODE_SCOPED_TABS.includes(activeTab);" in body

    def test_a_node_never_sees_a_picker(self):
        """On a node there is exactly one Xray and it is the local one."""

        body = _system()

        assert "!isWorker &&" in body
        assert "if (isWorker) return;" in body, "the auto-select effect must not run on a node"

    def test_a_destructive_action_names_what_it_will_restart(self):
        """One button, several possible targets: the confirmation has to say which."""

        body = _flat(_system())

        assert "constxrayTargetName" in body
        assert "Restartthexray" not in body.lower().replace("`", "")
        assert "${xrayTargetName}" in body


class TestTheDashboardCanRouteAUserOnANode:
    def test_the_route_button_is_no_longer_gated_on_a_local_xray(self):
        """`Dashboard.tsx` already sent `?panel_id=`; the button that would send it never rendered."""

        body = _flat(_dashboard())

        assert "onClick={()=>setRoutingModal(true)}" in body
        assert '{hasLocalXray&&(<Buttonvariant="secondary"size="icon"' not in body

    def test_the_route_request_still_carries_the_panel(self):
        body = _flat(_dashboard())

        assert _flat("api.post(`/user/routing${panelQs}`") in body

    def test_the_choices_come_from_the_node_that_will_do_the_routing(self):
        """The master holds no outbounds of its own since 4c-2 -- an unscoped list is an empty list."""

        body = _flat(_dashboard())

        assert _flat("api.get<Outbound[]>(`/outbounds${panelQs}`)") in body
        assert _flat("api.get<Balancer[]>(`/balancers${panelQs}`)") in body
        assert _flat("queryKey: ['outbounds', routeScope]") in body
        assert _flat("queryKey: ['balancers', routeScope]") in body

    def test_the_lists_are_not_fetched_until_the_modal_opens(self):
        """One query per user row per render is the cost of getting this wrong."""

        body = _flat(_dashboard())

        assert body.count("enabled:routingModal") == 2
