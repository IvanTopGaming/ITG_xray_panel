"""§29/§50: the child side of federation finally has an interface, and only on a node.

Until this wave no bundle called `POST /api/federation/link-token` at all — §29 said the card lived
in `packages/admin/src/pages/Panels.tsx`, and §50 corrected that: what lives there is the master's
*paste* field. So the only way to link a node was `curl` against its API with an admin JWT, and the
only way to revoke a token was editing `federation_config` over SSH.

The card sits in `ui-core`, which both frontend images ship, so the role gate is what keeps it off
the master. That gate has to hold at two levels and this guard checks both:

* the tab and its body render only under `isWorker`;
* the `GET /api/federation/config` query is `enabled: isWorker`, so a master never even calls a
  route it stopped registering in wave 0 — the alternative is a 404 on every visit to System.

A third level exists in the backend and is not this file's business: `roles/master.py` registers no
`federation` blueprint, so even a tampered `panel-role` meta tag reaches nothing.
"""

from tests.frontend_import_graph import PACKAGE_ROOTS

SYSTEM_PAGE = PACKAGE_ROOTS["ui-core"] / "pages" / "System.tsx"
PANELS_PAGE = PACKAGE_ROOTS["admin"] / "pages" / "Panels.tsx"


def _system():
    return SYSTEM_PAGE.read_text(encoding="utf-8")


def _panels():
    return PANELS_PAGE.read_text(encoding="utf-8")


def test_the_node_card_issues_a_link_token():
    body = _system()

    assert "'/federation/link-token'" in body
    assert "'/federation/config'" in body


def test_the_card_is_gated_to_a_node_at_both_levels():
    body = _system()

    assert "isWorker ? [{ id: 'federation'" in body, "the tab itself must be node-only"
    assert "{isWorker && activeTab === 'federation' &&" in body, "so must its body"
    assert "enabled: isWorker," in body, "a master must not call a route it does not register"


def test_the_card_warns_before_it_revokes():
    body = _system()

    assert "confirmRevoke" in body
    assert "Revoke access & issue token" in body


def test_the_master_can_relink_an_existing_panel_without_unlinking_it():
    body = _panels()

    assert "/relink" in body
    assert "relinkPanelMutation" in body


def test_the_master_page_still_has_no_child_side_of_its_own():
    body = _panels()

    assert "/federation/link-token" not in body, (
        "the master is never a child (§8.2) and registers no federation blueprint — a card here "
        "would call a route that answers 404"
    )
