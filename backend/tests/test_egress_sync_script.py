import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "egress-sync.sh"

PLAN_TWO = (
    '[{"tag":"a","public_ip":"203.0.113.10","send_through":"172.28.0.128","gateway":""},'
    '{"tag":"b","public_ip":"198.51.100.9","send_through":"172.28.0.129","gateway":"198.51.100.1"}]'
)

PLAN_BAD_GATEWAY = (
    '[{"tag":"a","public_ip":"203.0.113.10","send_through":"172.28.0.128","gateway":"10.99.99.1"},'
    '{"tag":"b","public_ip":"198.51.100.9","send_through":"172.28.0.129","gateway":"198.51.100.1"}]'
)


def _stub(bin_dir, name, body):
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


JUMP_FIRST = "-P POSTROUTING ACCEPT\n-A POSTROUTING -j EGRESS_SNAT\n"
JUMP_BEHIND_DOCKER = (
    "-P POSTROUTING ACCEPT\n-A POSTROUTING -s 172.17.0.0/16 ! -o docker0 -j MASQUERADE\n-A POSTROUTING -j EGRESS_SNAT\n"
)


def _host(
    tmp_path,
    plan,
    *,
    existing_addrs="",
    owned="",
    snat="",
    rules="",
    postrouting="",
    ip_fails="",
    iptables_fails="",
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    _stub(bin_dir, "curl", f"printf %s {plan!r}\n" if plan is not None else "exit 7\n")
    _stub(
        bin_dir,
        "ip",
        'printf "ip %s\\n" "$*" >> "$CALL_LOG"\n'
        'case "$*" in\n'
        '  "route show default") printf "default via 192.0.2.1 dev eth0\\n" ;;\n'
        '  "-4 -o addr show dev eth0") printf %s "$IP_ADDR_OUT" ;;\n'
        '  "rule list") printf %s "$IP_RULES" ;;\n'
        "esac\n" + (f'case "$*" in {ip_fails}) exit 2 ;; esac\n' if ip_fails else "") + "exit 0\n",
    )
    _stub(
        bin_dir,
        "iptables",
        'printf "iptables %s\\n" "$*" >> "$CALL_LOG"\n'
        'case "$*" in\n'
        '  "-t nat -S EGRESS_SNAT") printf %s "$IPT_RULES" ;;\n'
        '  "-t nat -S POSTROUTING") printf %s "$IPT_POSTROUTING" ;;\n'
        "esac\n" + (f'case "$*" in {iptables_fails}) exit 1 ;; esac\n' if iptables_fails else "") + "exit 0\n",
    )
    _stub(bin_dir, "docker", 'printf "172.28.0.5\\n"\n')
    _stub(bin_dir, "id", 'printf "0\\n"\n')

    dir_ = tmp_path / "deploy"
    dir_.mkdir()
    (dir_ / ".env").write_text("EGRESS_INTERNAL_TOKEN=tok\n")
    (dir_ / "egress.conf").write_text("EGRESS_UPLINK_IFACE=eth0\nEGRESS_PLAN_URL=http://panel/plan\n")
    if owned:
        (dir_ / "egress-owned").write_text(owned)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["CALL_LOG"] = str(log)
    env["IP_ADDR_OUT"] = existing_addrs
    env["IP_RULES"] = rules or "0:\tfrom all lookup local\n"
    env["IPT_RULES"] = snat
    env["IPT_POSTROUTING"] = postrouting or JUMP_FIRST
    return dir_, log, env


SETTLED_ADDRS = "1: eth0 inet 203.0.113.10/32 scope global eth0\n2: eth0 inet 198.51.100.9/32 scope global eth0\n"
SETTLED_SNAT = (
    "-N EGRESS_SNAT\n"
    "-A EGRESS_SNAT -s 172.28.0.128/32 -j SNAT --to-source 203.0.113.10\n"
    "-A EGRESS_SNAT -s 172.28.0.129/32 -j SNAT --to-source 198.51.100.9\n"
)
SETTLED_RULES = "0:\tfrom all lookup local\n30000:\tfrom 198.51.100.9 lookup 100\n"


def _run(dir_, env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), "--dir", str(dir_), *args],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_it_raises_every_planned_address_and_snats_it(tmp_path):
    dir_, log, env = _host(tmp_path, PLAN_TWO, existing_addrs="")
    result = _run(dir_, env)
    assert result.returncode == 0, result.stderr
    calls = log.read_text()

    assert "ip addr add 203.0.113.10/32 dev eth0" in calls
    assert "ip addr add 198.51.100.9/32 dev eth0" in calls
    assert "iptables -t nat -F EGRESS_SNAT" in calls, (
        "the chain is reloaded whole on every pass; appending without flushing would "
        "accumulate a duplicate SNAT rule per run and never converge."
    )
    assert "iptables -t nat -A EGRESS_SNAT -s 172.28.0.128 -j SNAT --to-source 203.0.113.10" in calls
    assert (dir_ / "egress-owned").read_text().split() == ["203.0.113.10", "198.51.100.9"], (
        "an address the synchroniser raised must be recorded, or it can never tell its own "
        "aliases from the machine's primary address and would eventually delete the wrong one."
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_an_address_already_on_the_interface_is_not_claimed(tmp_path):
    dir_, log, env = _host(tmp_path, PLAN_TWO, existing_addrs="1: eth0 inet 203.0.113.10/24 scope global eth0\n")
    assert _run(dir_, env).returncode == 0
    calls = log.read_text()

    assert "ip addr add 203.0.113.10/32" not in calls
    owned = (dir_ / "egress-owned").read_text().split()
    assert owned == ["198.51.100.9"], (
        "the provider may hand the address out by DHCP or netplan. Claiming it would let a "
        "later unbind delete an address the synchroniser never created."
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_gateway_gets_policy_routing_inside_the_owned_range(tmp_path):
    dir_, log, env = _host(tmp_path, PLAN_TWO)
    assert _run(dir_, env).returncode == 0
    calls = log.read_text()

    assert "ip route add default via 198.51.100.1 dev eth0 table 100" in calls
    assert "ip rule add from 198.51.100.9 table 100 priority 30000" in calls
    assert "from 203.0.113.10 table" not in calls, (
        "an entry with no gateway needs no routing table — its address is on the uplink's own subnet."
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_an_empty_plan_removes_what_we_own(tmp_path):
    dir_, log, env = _host(tmp_path, "[]", owned="203.0.113.10\n")
    assert _run(dir_, env).returncode == 0
    calls = log.read_text()

    assert "ip addr del 203.0.113.10/32 dev eth0" in calls
    assert (dir_ / "egress-owned").read_text().strip() == ""


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_an_unreachable_panel_leaves_the_host_alone(tmp_path):
    dir_, log, env = _host(tmp_path, None, owned="203.0.113.10\n")
    result = _run(dir_, env)

    assert result.returncode == 1
    assert not log.exists() or "addr del" not in log.read_text(), (
        "no answer and an empty answer must not converge on the same action. A node whose "
        "backend is a few seconds slower than the boot timer would otherwise drop every "
        "customer's dedicated address on each reboot."
    )
    assert (dir_ / "egress-owned").read_text().strip() == "203.0.113.10"


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_a_settled_host_is_left_completely_alone(tmp_path):
    dir_, log, env = _host(
        tmp_path,
        PLAN_TWO,
        existing_addrs=SETTLED_ADDRS,
        owned="203.0.113.10\n198.51.100.9\n",
        snat=SETTLED_SNAT,
        rules=SETTLED_RULES,
        postrouting=JUMP_FIRST,
    )
    assert _run(dir_, env).returncode == 0
    calls = log.read_text()

    for forbidden in ("addr add", "addr del", "-t nat -F", "-t nat -A", "rule add", "rule del", "-t nat -D"):
        assert forbidden not in calls, (
            f"a pass over a host that already matches the plan issued `{forbidden}`. The timer "
            f"runs every 30 seconds: an unconditional rewrite churns netfilter forever and makes "
            f"--dry-run permanently non-empty, so doctor could never tell a settled host from a "
            f"stalled one."
        )


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_dry_run_on_a_settled_host_prints_nothing(tmp_path):
    dir_, _, env = _host(
        tmp_path,
        PLAN_TWO,
        existing_addrs=SETTLED_ADDRS,
        owned="203.0.113.10\n198.51.100.9\n",
        snat=SETTLED_SNAT,
        rules=SETTLED_RULES,
        postrouting=JUMP_FIRST,
    )
    result = _run(dir_, env, "--dry-run")

    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        "doctor reads this output to decide whether the host is in step. Anything printed on a "
        "settled host is a permanent false alarm."
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_a_routing_table_that_does_not_exist_yet_still_gets_its_route(tmp_path):
    dir_, log, env = _host(tmp_path, PLAN_TWO, ip_fails='"route flush table 100"')
    assert _run(dir_, env).returncode == 0
    calls = log.read_text()

    assert "ip route add default via 198.51.100.1 dev eth0 table 100" in calls, (
        "`ip route flush table N` exits 2 with 'FIB table does not exist' until something has "
        "been added to that table, which is every host's state on its first pass. Letting that "
        "abort the run leaves policy routing permanently unconfigured, because the route that "
        "would bring the table into existence is the very next command."
    )
    assert "ip rule add from 198.51.100.9 table 100 priority 30000" in calls


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_a_fault_midway_never_claims_the_host_was_left_alone(tmp_path):
    dir_, log, env = _host(
        tmp_path,
        PLAN_TWO,
        iptables_fails='"-t nat -A EGRESS_SNAT -s 172.28.0.128 -j SNAT --to-source 203.0.113.10"',
    )
    result = _run(dir_, env)

    assert result.returncode == 2, (
        "exit 1 is the caller's signal that the panel did not answer and nothing was changed — "
        "install.sh renders it as 'the host was left as it was'. A fault that lands after the "
        "first mutation must not borrow that code: here two addresses are already up and the "
        "SNAT chain has just been flushed empty, so every dedicated address is silently off."
    )
    assert "ip addr add 203.0.113.10/32 dev eth0" in log.read_text()


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_a_non_root_run_refuses_before_touching_anything(tmp_path):
    dir_, log, env = _host(tmp_path, PLAN_TWO)
    _stub(tmp_path / "bin", "id", 'printf "1000\\n"\n')
    result = _run(dir_, env)

    assert result.returncode == 2, (
        "exit 1 is reserved for 'the panel did not answer and the host is untouched', which the "
        "installer renders as reassurance. A caller error must not borrow it."
    )
    assert not log.exists(), (
        "the refusal has to land before the first command. Without the gate the run gets as far "
        "as `ip addr add` and the operator reads 'RTNETLINK answers: Operation not permitted' "
        "with no clue which of the four privileged things the script needs was refused."
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_a_rule_inserted_ahead_of_our_jump_moves_it_back_to_the_front(tmp_path):
    dir_, log, env = _host(
        tmp_path,
        PLAN_TWO,
        existing_addrs=SETTLED_ADDRS,
        owned="203.0.113.10\n198.51.100.9\n",
        snat=SETTLED_SNAT,
        rules=SETTLED_RULES,
        postrouting=JUMP_BEHIND_DOCKER,
    )
    assert _run(dir_, env).returncode == 0
    calls = log.read_text()

    assert "iptables -t nat -D POSTROUTING -j EGRESS_SNAT" in calls, (
        "docker rewrites nat/POSTROUTING on `docker network create` and on daemon restart, "
        "putting its own MASQUERADE ahead of ours. Checking only that the jump is present "
        "leaves every dedicated address masqueraded back to the primary one — traffic leaves "
        "on the wrong IP while the chain, the aliases and the routing all still look right."
    )
    assert calls.index("iptables -t nat -D POSTROUTING -j EGRESS_SNAT") < calls.index(
        "iptables -t nat -I POSTROUTING -j EGRESS_SNAT"
    ), "inserting before deleting leaves two jumps in POSTROUTING, and the stray one is never cleaned up."


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_a_host_with_no_chain_yet_creates_it_and_jumps_into_it(tmp_path):
    dir_, log, env = _host(
        tmp_path,
        PLAN_TWO,
        iptables_fails='"-t nat -n -L EGRESS_SNAT"|"-t nat -C POSTROUTING -j EGRESS_SNAT"',
    )
    assert _run(dir_, env).returncode == 0
    calls = log.read_text()

    assert "iptables -t nat -N EGRESS_SNAT" in calls, (
        "this is every node's state on its first pass. Without the chain the SNAT rules have "
        "nowhere to go, and `-A EGRESS_SNAT` fails on a chain that does not exist."
    )
    assert "iptables -t nat -I POSTROUTING -j EGRESS_SNAT" in calls, (
        "a populated chain nothing jumps into is never consulted: every dedicated address is "
        "configured, looks configured, and does nothing."
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_one_unroutable_gateway_does_not_strand_the_entries_after_it(tmp_path):
    dir_, log, env = _host(
        tmp_path,
        PLAN_BAD_GATEWAY,
        ip_fails='"route add default via 10.99.99.1 dev eth0 table 100"',
    )
    result = _run(dir_, env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text()

    assert "ip rule add from 198.51.100.9 table 101 priority 30001" in calls, (
        "the rebuild tears down the whole 30000-30099 range first, so a gateway the kernel "
        "refuses ('Nexthop has invalid gateway' on an operator typo the panel cannot catch — "
        "it validates the syntax, not that the address is on-link) would abort the run before "
        "the entries after it are restored. Under a 30-second timer that is a permanent outage "
        "for customers who have nothing to do with the typo."
    )
    assert "ip rule add from 203.0.113.10 table 100" not in calls, (
        "an entry whose default route was refused must not get a rule pointing at the empty table."
    )
    assert "gateway 10.99.99.1" in result.stderr, (
        "the address that failed has to be named, or the operator sees a working host with one "
        "silently unrouted customer and nothing to grep for."
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="synchroniser not written yet")
def test_dry_run_touches_nothing(tmp_path):
    dir_, log, env = _host(tmp_path, PLAN_TWO)
    result = _run(dir_, env, "--dry-run")

    assert result.returncode == 0
    assert "+ ip addr add 203.0.113.10/32 dev eth0" in result.stdout
    assert not (dir_ / "egress-owned").exists()
    calls = log.read_text() if log.exists() else ""
    assert "addr add" not in calls and "-t nat -A" not in calls
