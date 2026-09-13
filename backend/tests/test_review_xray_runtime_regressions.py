import os
import subprocess
import sys
from pathlib import Path


def test_isolated_xray_runtime_and_traffic_regressions():
    backend = Path(__file__).resolve().parents[1]
    suite = backend / "reliability_tests"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(suite / "test_xray_regressions.py"),
            str(suite / "test_runtime_apply.py"),
            str(suite / "test_traffic_recovery.py"),
            str(suite / "test_xray_worker_regressions.py"),
            f"--confcutdir={suite}",
            "-q",
        ],
        cwd=backend,
        env={**os.environ, "PANEL_ROLE": "worker"},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
