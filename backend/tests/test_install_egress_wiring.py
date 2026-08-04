from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = (REPO_ROOT / "scripts" / "install.sh").read_text()


def test_compose_carries_the_egress_profile_once_configured():
    assert "--profile egress" in INSTALL, (
        "the sidecar sits behind a compose profile, so update and doctor must pass it too. "
        "Without it `compose ps` reports the sidecar as absent on a host that runs it."
    )
    assert "COMPOSE_PROFILES" not in INSTALL, (
        "routing the profile through .env would put a variable in .env.node.example that "
        "docker-compose.node.yml never references, which test_env_examples.py forbids."
    )


def test_the_synchroniser_is_installed_beside_the_deployment():
    assert "egress-sync.sh" in INSTALL
    assert "panel-egress-sync.timer" in INSTALL
    assert "OnUnitActiveSec" in INSTALL, "a unit without a timer covers the reboot but not an edit made in the web UI."


def test_the_host_settings_never_enter_dot_env():
    assert "EGRESS_UPLINK_IFACE" in INSTALL
    for line in INSTALL.splitlines():
        if "EGRESS_UPLINK_IFACE" in line:
            assert "env_set" not in line, (
                "EGRESS_UPLINK_IFACE is read by the host unit, not by any container. Writing it "
                "into .env would make the node's example file fail the guard that every variable "
                "it defines is referenced by its own compose file."
            )


def test_apply_tells_an_untouched_host_from_a_partly_converged_one():
    body = INSTALL.split("egress_apply()")[1].split("\n}")[0]
    assert "1)" in body and "*)" in body, (
        "the synchroniser exits 1 only when it reached nothing and 2 when it stopped after "
        "mutating. Collapsing them prints 'the host was left as it was' about a host whose "
        "SNAT chain has just been flushed empty."
    )


def test_egress_is_a_command_and_a_menu_entry():
    assert "cmd_egress" in INSTALL
    assert "egress) cmd_egress ;;" in INSTALL, "the command must be reachable non-interactively"
    assert "role_line 5 egress" in INSTALL, (
        "an operator who runs the installer with no arguments must find it without knowing the subcommand exists."
    )


def test_the_menu_entry_is_offered_on_a_node_only():
    guarded = INSTALL.split("role_line 5 egress")[0].rstrip().splitlines()[-1]
    assert "ROLE" in guarded and "node" in guarded, (
        "only a node runs Xray, so only a node has an egress to manage. Offering it on the "
        "master would open a menu whose every action 501s."
    )
    assert INSTALL.count('die "egress addresses live on a node"') == 1, (
        "the non-interactive path takes no menu, so the command itself must refuse too."
    )


def test_the_primary_address_is_excluded_from_the_free_list():
    assert "egress_primary" in INSTALL, (
        "the machine's own address shows up in `ip addr show` like any other. Offering it as "
        "free invites an operator to SNAT the host's primary address to one customer."
    )
    body = INSTALL.split("egress_rows()")[1].split("\n}")[0]
    assert "primary" in body and "continue" in body, "the primary address must be skipped, not merely computed"


def test_binding_creates_a_freedom_outbound_through_the_panel():
    body = INSTALL.split("egress_bind()")[1].split("\negress_unbind()")[0]
    assert "/outbounds" in body and '"freedom"' in body
    assert "send_through" not in body, (
        "the internal bind address is allocated by the panel (_assign_egress_fields → "
        "allocate_bind_ip). Sending one from here would race the panel's own pool."
    )


def test_binding_verifies_the_address_and_says_what_it_proved():
    body = INSTALL.split("egress_bind()")[1].split("\negress_unbind()")[0]
    assert "--interface" in body, "a bind that is never verified is how this feature failed silently before"
    assert "from the host" in body, (
        "the check runs from the host, so it proves the address is usable — not that the "
        "customer's traffic already leaves through it. The wording must not claim the second."
    )


def test_unbinding_reports_the_clients_it_moved_without_asking_first():
    body = INSTALL.split("egress_unbind()")[1].split("\ncmd_egress()")[0]
    assert "api_delete" in body
    assert "client" in body, (
        "the panel silently nulls preferred_outbound on every client using the outbound. "
        "Deleting without saying how many were moved leaves the operator to find out from them."
    )
    assert "are you sure" not in body.lower() and "confirm" not in body.lower(), (
        "the operator asked for a straight delete: no confirmation step."
    )


def test_doctor_reports_egress_when_it_is_configured():
    assert "check_egress" in INSTALL
    assert INSTALL.index("check_egress") < INSTALL.index("check_monitoring()"), (
        "doctor must call it; a checker defined and never invoked is the same silence this whole feature suffered from."
    )
    body = INSTALL.split("check_egress()")[1].split("\n}")[0]
    assert "--dry-run" in body, (
        "the honest way to say 'the host is in step' is to ask the synchroniser what it would "
        "still change. Reporting only that the timer is active would call a stalled host healthy."
    )
    assert "is-active" in body
