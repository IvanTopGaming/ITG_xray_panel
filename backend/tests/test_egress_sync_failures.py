import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "xray-egress" / "sync.sh"


@pytest.mark.parametrize("reply,exit_code", [("", 7), ("not-json", 0), ('{"unexpected":true}', 0)])
def test_failed_egress_poll_is_visible_and_does_not_mutate_addresses(tmp_path, reply, exit_code):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "curl": 'printf "%s" "$REPLY"\nexit "$CURL_EXIT"',
        "ip": 'echo "$*" >> "$IP_CALLS"',
        "sleep": "exit 99",
    }.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    calls = tmp_path / "calls"
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=5,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REPLY": reply,
            "CURL_EXIT": str(exit_code),
            "XRAY_IFACE": "eth0",
            "IP_CALLS": str(calls),
        },
    )
    assert result.returncode != 0
    assert "egress" in result.stderr.lower()
    assert not calls.exists()
