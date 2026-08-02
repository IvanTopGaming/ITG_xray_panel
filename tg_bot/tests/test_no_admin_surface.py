"""Wave 4a: the bot stops talking to the admin API, because there is no admin API to talk to.

Phase 3c-2 pointed the bot at bot-api and deleted `admin_proxy.py`, but left `api_service.py`
written against the monolith's admin surface. bot-api serves 15 routes and none of them are the
six that file called, so every request 404'd and three user-facing screens answered "no keys",
"unavailable" and "No active key found" to users who had working subscriptions.

The fix is not to give bot-api those endpoints. It is to stop needing them: everything the user
screens show now arrives in one `/bot-service/users/<id>/state` response.
"""

import pathlib

import pytest

BOT_ROOT = pathlib.Path(__file__).resolve().parents[1]

DEAD_ADMIN_PATHS = (
    "api/inbounds",
    "api/panels",
    "api/stats/system",
    "api/restart",
    "api/backup",
    "api/restore",
    "api/sub/",
)


def _sources():
    return [
        path
        for path in BOT_ROOT.rglob("*.py")
        if "tests" not in path.parts and ".venv" not in path.parts and "__pycache__" not in path.parts
    ]


def test_api_service_is_gone():
    assert not (BOT_ROOT / "api_service.py").exists(), (
        "api_service.py builds a MultiPanelManager against endpoints bot-api does not serve. Its "
        "dedup-by-endpoint logic existed because several monolith panels returned overlapping links; "
        "bot-api builds one link per (inbound, client) pair from the snapshot, so there is nothing to "
        "deduplicate."
    )
    assert not (BOT_ROOT / "handlers" / "admin.py").exists(), (
        "handlers/admin.py drives backup, restore, restart and server listing — fleet management, which "
        "belongs in the master panel and nowhere else."
    )


def test_no_module_imports_the_deleted_manager():
    offenders = sorted(
        str(path.relative_to(BOT_ROOT))
        for path in _sources()
        if "api_service" in path.read_text() or "panel_api" in path.read_text()
    )
    assert offenders == [], f"these modules still reference the deleted panel manager: {offenders}"


@pytest.mark.parametrize("dead_path", DEAD_ADMIN_PATHS)
def test_no_module_reaches_the_admin_api(dead_path):
    offenders = sorted(str(path.relative_to(BOT_ROOT)) for path in _sources() if dead_path in path.read_text())
    assert offenders == [], (
        f"{offenders} still build a request to {dead_path!r}, which bot-api does not serve. "
        "That is the exact shape of the regression: a 404 the bot renders as an empty screen."
    )


def test_the_admin_only_fsm_states_are_gone():
    import states

    assert not hasattr(states, "RestoreStates"), (
        "RestoreStates drove a database upload straight into a chat message. Restore is an emergency "
        "path; it belongs behind an admin JWT in the panel, not behind a Telegram file upload."
    )
    assert not hasattr(states, "BackupStates")
    assert not hasattr(states.UserStates, "viewing_qr"), (
        "the QR server picker is gone — each client record already names its own server, so picking one "
        "again was a step that asked the user to repeat what they had just chosen."
    )


def test_the_admin_keyboards_are_gone():
    import keyboards

    for name in (
        "admin_main_kb",
        "admin_backups_kb",
        "admin_restore_type_kb",
        "server_selection_kb",
        "confirm_restart_kb",
        "admin_back_kb",
        "user_qr_server_kb",
    ):
        assert not hasattr(keyboards, name), f"keyboards.{name} survived; it has no handler left to serve"


def test_the_router_set_no_longer_includes_admin():
    source = (BOT_ROOT / "main.py").read_text()
    assert "admin" not in source.split("def main")[0], "main.py still imports the admin router"
    assert "include_router(admin.router)" not in source
