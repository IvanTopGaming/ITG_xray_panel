"""Task 15: the admin surface for node transfer.

`GET /panels` overlays `status` from the Redis liveness marker (`get_panel_liveness`, called in
`list_panels`) *after* reading `LinkedPanel.to_dict()`, and `poll_linked_panels` writes that marker
to `"offline"` on every failed poll -- which is exactly what a transferring node looks like from the
cron host's point of view until the A record moves. So a `status === 'transferring'` value computed
in `panel_core.jobs.panels._record` never survives to this endpoint's response; the frontend has
nothing to color by there. `transfer_state` is not touched by that overlay and does reach the
frontend unmasked (`LinkedPanel.to_dict()` in `models.py`, read verbatim by `list_panels`), so the
card must key off `panel.transfer_state === 'awaiting_dns'`, not `panel.status`.

These are string-level checks on the built TSX source, the same style already used by
`test_system_page_reaches_the_nodes.py` and friends -- there is no TSX runtime in the backend test
suite to exercise the component directly.
"""

import re

from tests.frontend_import_graph import PACKAGE_ROOTS

TYPES_FILE = PACKAGE_ROOTS["ui-core"] / "lib" / "types.ts"
PANELS_PAGE = PACKAGE_ROOTS["admin"] / "pages" / "Panels.tsx"
SYSTEM_PAGE = PACKAGE_ROOTS["ui-core"] / "pages" / "System.tsx"


def _types():
    return TYPES_FILE.read_text(encoding="utf-8")


def _panels():
    return PANELS_PAGE.read_text(encoding="utf-8")


def _system():
    return SYSTEM_PAGE.read_text(encoding="utf-8")


def _flat(body):
    return re.sub(r"\s+", "", body)


class TestLinkedPanelCarriesTheTransferFields:
    def test_the_three_fields_are_typed(self):
        body = _types()

        assert "transfer_state: string;" in body
        assert "current_instance_id: string | null;" in body
        assert "superseded_at: number | null;" in body

    def test_the_transfer_token_result_shape_matches_the_endpoint(self):
        """`POST /panels/<id>/transfer-token` replies `{token, expires_at, state_freshness}`; a UI
        that reaches for a field this type does not declare fails to compile, not silently."""

        body = _types()

        assert "export interface TransferTokenResult" in body
        result = body.split("export interface TransferTokenResult", 1)[1].split("}", 1)[0]
        assert "token: string;" in result
        assert "expires_at: number;" in result
        assert "ok: boolean;" in result
        assert "taken_at: number | null;" in result


class TestTheCardColorsByTransferStateNotByStatus:
    def test_transferring_is_derived_from_transfer_state(self):
        body = _flat(_panels())

        assert "consteffectiveStatus=panel.transfer_state==='awaiting_dns'?'transferring':panel.status;" in body, (
            "the badge must key off transfer_state === 'awaiting_dns', not off the raw status field: "
            "poll_linked_panels overlays a failed poll's status to 'offline' in Redis, and list_panels "
            "reads that overlay after to_dict() -- a DB-side 'transferring' value never reaches this "
            "response, so status alone cannot tell a transfer apart from an outage"
        )

    def test_the_badge_and_icon_read_the_derived_status(self):
        body = _flat(_panels())

        assert "STATUS_ICON[effectiveStatus]" in body
        assert "STATUS_ICON[panel.status]" not in body, (
            "a leftover direct read of panel.status would show the transferring node as offline again"
        )

    def test_transferring_has_its_own_entry_distinct_from_offline(self):
        body = _panels()

        icon_block = body.split("const STATUS_ICON", 1)[1].split("};", 1)[0]
        assert "transferring:" in icon_block
        transferring_line = [ln for ln in icon_block.splitlines() if "transferring:" in ln][0]
        offline_line = [ln for ln in icon_block.splitlines() if "offline:" in ln][0]
        assert "text-amber-400" in transferring_line
        assert "text-red-400" in offline_line
        assert transferring_line != offline_line

    def test_the_transferring_plashka_is_not_the_offline_error_box(self):
        body = _flat(_panels())

        assert "{effectiveStatus==='transferring'&&(" in body
        assert "bg-amber-500/10" in _panels(), "the transfer plashka must not read as an incident"

    def test_the_last_error_box_is_suppressed_while_transferring(self):
        """Without this, a transferring node shows both the amber 'transferring' plashka and the red
        last_error box at once -- exactly the incident look Task 15 exists to avoid."""

        body = _flat(_panels())

        assert "panel.last_error&&effectiveStatus==='offline'&&(" in body
        assert "panel.last_error&&panel.status==='offline'&&(" not in body


class TestTheTransferDialog:
    def test_issuing_calls_the_transfer_token_endpoint(self):
        body = _flat(_panels())

        assert "api.post(`/panels/${data.id}/transfer-token`,{carry_admin:data.carry_admin})" in body

    def test_carry_admin_defaults_on(self):
        body = _panels()

        assert "const [carryAdmin, setCarryAdmin] = useState(true);" in body, (
            "carrying the node admin's account over is the documented default; flipping this silently "
            "changes what a replacement machine ends up with"
        )

    def test_freshness_is_shown_before_the_transfer_string(self):
        """Constraint from the brief: the admin must see whether the copy is fresh before they act on
        the secret string, not after. Both are rendered together once the token is issued, so the
        order inside that block is what 'before' means in practice."""

        body = _panels()

        freshness_at = body.index("transferResult.state_freshness.ok")
        secret_at = body.index("Transfer string")
        assert freshness_at < secret_at

    def test_a_dead_node_names_the_age_of_the_copy_it_will_use(self):
        body = _flat(_panels())

        assert "transferResult.state_freshness.taken_at" in body
        assert "formatDateTime(transferResult.state_freshness.taken_at)" in body

    def test_closing_clears_the_secret_from_state(self):
        """The transfer string is a one-shot secret: REALITY private key, every client UUID, the node
        admin's password hash. If closeTransfer stopped clearing transferResult, reopening the dialog
        on the same panel would still show the old (possibly already-claimed) string."""

        body = _panels()

        close_fn = body.split("const closeTransfer = () => {", 1)[1].split("};", 1)[0]
        assert "setTransferResult(null);" in close_fn
        assert "setTransferTarget(null);" in close_fn

    def test_the_modal_close_handler_is_the_clearing_one(self):
        body = _flat(_panels())

        assert "onClose={closeTransfer}" in body
        assert "isOpen={!!transferTarget}" in body


class TestSystemPageSurfacesTwoNodeOnlyBanners:
    def test_the_superseded_banner_is_gated_on_local_xray(self):
        """`GET /system/version` never dispatches on `panel_id` (it is not one of the six Xray-control
        handlers), so unlike the egress banner this one genuinely cannot be proxied to a picked node
        from the master -- it stays hasLocalXray-only, the same exemption already granted to the log
        panel and the Maintenance backup card."""

        body = _system()

        assert "const supersededAt = hasLocalXray ? (versionQuery.data?.running.superseded_at ?? null) : null;" in body
        assert "{showTransferBanners && (" in body

    def test_the_egress_banner_is_not_hard_gated_on_local_xray(self):
        """Unlike superseded_at, `/outbounds` already dispatches on `panel_id` -- Routing.tsx and
        Dashboard.tsx both proxy it to a picked node today. Hard-gating this banner on hasLocalXray
        would hide it from the master entirely, the same class of bug test_system_page_reaches_the_nodes
        exists to catch for the Core tab's settings query."""

        body = _flat(_system())

        assert "constshowTransferBanners=supersededAt!=null||strandedEgress.length>0;" in body

    def test_superseded_reads_the_role_state_handle_not_a_new_endpoint(self):
        """Task 15's correction: the marker rides the existing `/system/version` response
        (`useVersionStatus`'s underlying query), not a bespoke endpoint."""

        body = _flat(_system())

        assert "versionQuery.data?.running.superseded_at" in body
        assert "api.get('/system/superseded')" not in body
        assert "api.get(`/system/superseded" not in body

    def test_the_superseded_banner_is_red_and_says_replaced(self):
        body = _system()

        assert "This installation has been replaced" in body
        banner = body.split("supersededAt != null && (", 1)[1].split("strandedEgress.length > 0", 1)[0]
        assert "border-red-500/20" in banner
        assert "bg-red-500/10" in banner

    def test_the_egress_banner_matches_the_exact_leftover_shape(self):
        """Brief's condition, verbatim: non-empty send_through, empty public_ip, enable === false --
        precisely what a fresh install leaves behind on a dedicated-egress outbound after a transfer."""

        body = _flat(_system())

        assert "egressOutbounds.filter((o)=>!!o.send_through&&!o.public_ip&&o.enable===false)" in body

    def test_the_egress_banner_is_amber_not_red(self):
        body = _system()

        assert "Dedicated outgoing IPs are waiting for new addresses" in body
        banner = body.split("strandedEgress.length > 0 && (", 1)[1]
        assert "border-amber-500/20" in banner
        assert "bg-amber-500/10" in banner

    def test_the_egress_query_follows_the_pages_xray_scope(self):
        """Same rule as the neighbouring Xray-control queries on this page: wait for a scope, not for
        a local Xray -- otherwise the master never asks and the banner is invisible for every node."""

        body = _flat(_system())

        assert "enabled:xrayScopeResolved" in body
        assert "queryKey:['outbounds','egress-check',panelId]" in body
        assert "api.get<Outbound[]>(`/outbounds${xrayScope}`)" in body
        assert "enabled:hasLocalXray" not in body, (
            "a query still waits on hasLocalXray instead of the page's scope -- the exact bug "
            "test_system_page_reaches_the_nodes.py exists to catch for the settings query"
        )

    def test_the_egress_banner_names_the_node_when_viewed_from_the_master(self):
        """On a node the banner is about the box the admin is already looking at, so 'this panel' would
        be redundant; from the master, picking between several nodes, the banner is meaningless without
        saying which one it is about."""

        body = _flat(_system())

        assert "hasLocalXray?'':`on${xrayTargetName}`" in body

    def test_the_egress_banner_points_at_the_page_that_actually_edits_outbounds(self):
        """Outbounds are edited on Routing, not on a dedicated 'Outbounds' page -- Sidebar.tsx has no
        such entry. Sending an admin there would be the banner's only instruction, and it would be
        wrong."""

        body = _system()

        assert "address on the Routing page" in body
        assert "Outbounds page" not in body

    def test_the_banner_container_does_not_distort_the_masters_single_column_layout(self):
        """On the master the grid is `grid-cols-1 ... max-w-xl`, not the node's three-column layout.
        `lg:col-span-3` there creates two zero-width implicit columns and eats two 2rem gaps off the
        settings card; `col-span-full` spans whatever the grid actually has."""

        body = _system()

        assert 'className="col-span-full space-y-3"' in body
        assert "lg:col-span-3" not in body


class TestTheDialogShowsReachabilityBeforeIssuing:
    def test_the_pre_issue_view_shows_the_cards_own_status_and_last_poll(self):
        """Brief Step 2 and the spec both want freshness shown before the admin acts, not after.
        `state_freshness` only exists once the key is already issued (`refresh_mirror_live` runs
        inside `issue_transfer_token`, there is no separate probe endpoint), so the honest proxy
        available before that click is the reachability data the card already carries."""

        body = _flat(_panels())

        assert "STATUS_ICON[transferTarget?.status||'unknown']" in body
        assert "formatLastPoll(transferTarget?.last_poll??null)" in body

    def test_the_reachability_block_precedes_the_issue_button(self):
        body = _panels()

        status_at = body.index("Node status right now")
        issue_button_at = body.index("Issue transfer key")
        assert status_at < issue_button_at


class TestTheFreshnessTextHasThreeHonestBranches:
    def test_a_never_mirrored_panel_gets_its_own_branch(self):
        """`state_freshness.taken_at is None` means panel_transfer.read_current() found no row at
        all -- not 'stale', but 'nothing to transfer'. claim_transfer's `_state_reply` answers such a
        claim with 409 'no state mirrored for this panel yet', so a key issued in this state is
        useless; before this fix the UI printed 'taken at an unknown time', promising a transfer that
        cannot happen."""

        body = _flat(_panels())

        assert (
            "transferResult.state_freshness.ok?(" in body and ":transferResult.state_freshness.taken_at!=null?(" in body
        ), "the three branches must be ok / stale-but-mirrored / never-mirrored, in that order"
        assert "neverbeenmirrored" in body.replace(" ", "").lower()
        assert "atanunknowntime" not in body.lower()

    def test_the_stale_branch_no_longer_needs_an_unknown_time_fallback(self):
        """Reachable only when taken_at is guaranteed non-null by the branch above it."""

        body = _flat(_panels())

        assert "formatDateTime(transferResult.state_freshness.taken_at)}." in body


class TestClosingDuringAnInFlightIssueCannotOrphanTheKey:
    def test_closing_is_a_no_op_while_the_request_is_in_flight(self):
        """Without this, Escape/backdrop/X/Cancel during the round-trip closes the dialog, but
        onSuccess still lands and calls setTransferResult on an already-closed dialog -- a live
        hour-long key sits on the master that nobody ever saw on screen."""

        body = _panels()

        close_fn = body.split("const closeTransfer = () => {", 1)[1].split("};", 1)[0]
        assert close_fn.strip().startswith("if (transferMutation.isPending) return;")

    def test_cancel_is_disabled_while_pending(self):
        body = _flat(_panels())

        assert "onClick={closeTransfer}disabled={transferMutation.isPending}>Cancel" in body

    def test_closing_evicts_the_secret_from_the_mutation_cache_too(self):
        """setTransferResult(null) only clears the component's own useState; the mutation's response
        -- which carries data.token -- survives in react-query's MutationCache (for the component's
        whole mounted lifetime, then gcTime beyond that) unless reset() is called too."""

        body = _panels()

        close_fn = body.split("const closeTransfer = () => {", 1)[1].split("};", 1)[0]
        assert "transferMutation.reset();" in close_fn
