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
