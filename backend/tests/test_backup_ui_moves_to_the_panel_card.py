"""§8.8 + §19: backing a node up is a button on the master, and the master stops offering its own.

The backend half of this wave restores a capability. The frontend half **creates** one: the four
proxy handlers in `panels.py` — `system-stats`, `restart`, `backup`, `restore` — had no caller in
any of the four bundles at all (`Panels.tsx` called only `PUT /panels/<id>`, `relink`, `DELETE` and
`test`). Their one consumer was the bot's admin menu, deleted in wave 4a. So "fix the 401" would
have left the wave with a working endpoint and no way to reach it, which is exactly the shape §50
corrected in wave 4b.

The `System.tsx` half is the other direction. Its Backup/Restore buttons call `/backup` and
`/restore` on the panel the browser is talking to. On the master those routes no longer exist, and
before this wave they answered `404 "DB not found"` and a `{"status": "restored"}` that had restored
nothing (§7.10). The buttons therefore have to disappear on a role with no local Xray, and say what
replaced them — an admin who remembers the button will otherwise go looking for it.

`System.tsx` ships from `ui-core`, which both frontend images carry, so `hasLocalXray` is the whole
gate on that side; the backend gate is that `roles/master.py` registers no `backup` blueprint, and
that is asserted in `test_node_backup_over_federation.py`, not here.
"""

from tests.frontend_import_graph import PACKAGE_ROOTS

SYSTEM_PAGE = PACKAGE_ROOTS["ui-core"] / "pages" / "System.tsx"
PANELS_PAGE = PACKAGE_ROOTS["admin"] / "pages" / "Panels.tsx"


def _system():
    return SYSTEM_PAGE.read_text(encoding="utf-8")


def _panels():
    return PANELS_PAGE.read_text(encoding="utf-8")


def test_the_master_can_download_a_nodes_backup():
    body = _panels()

    assert "/backup`, { responseType: 'blob' }" in body, (
        "the wave's whole point is that the file arrives in the admin's browser"
    )
    assert "downloadBackup" in body


def test_the_master_can_put_a_backup_back():
    body = _panels()

    assert "/restore`" in body
    assert "restorePanelMutation" in body


def test_restoring_a_node_asks_for_its_name_first():
    """Panel cards look alike; pouring one node's database into another cannot be undone."""

    body = _panels()

    assert "restoreConfirmName" in body
    assert "restoreConfirmName.trim() !== restoreTarget?.name" in body, (
        "the confirm button must stay disabled until the typed name matches the panel"
    )


def test_the_backup_buttons_are_gone_from_a_panel_that_has_no_sqlite_file():
    body = _system()

    assert "{hasLocalXray ? (" in body, "Backup/Restore must sit behind the local-Xray gate"
    assert "pg-backup" in body, (
        "an admin who remembers the button has to be told what replaced it, or they will look for "
        "a backup of the Postgres data tier in a panel that never had one"
    )


def test_the_system_page_still_backs_a_node_up_from_the_node_itself():
    body = _system()

    assert "api.get('/backup'" in body
    assert "api.post('/restore'" in body
