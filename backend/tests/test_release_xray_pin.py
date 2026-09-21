import json
import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml


REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("workflow", ["release.yml", "dev-build.yml"])
def test_worker_build_uses_the_same_xray_release_for_binary_and_protobuf(tmp_path, workflow):
    document = yaml.safe_load((REPO / ".github/workflows" / workflow).read_text())
    script = next(
        step["run"]
        for job in document["jobs"].values()
        for step in job["steps"]
        if "docker buildx build" in step.get("run", "")
    )
    versions = json.loads((REPO / "versions.json").read_text())
    versions["xray_core_ref"] = "v99.8.7"
    (tmp_path / "versions.json").write_text(json.dumps(versions))
    executable = tmp_path / "bin/docker"
    executable.parent.mkdir()
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['BUILD_ARGS_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    executable.chmod(0o755)
    log = tmp_path / "build.jsonl"
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{executable.parent}:{os.environ['PATH']}",
            "BUILD_ARGS_LOG": str(log),
            "BUMPED": "worker",
            "SHA": "0123456789abcdef",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    worker = next(json.loads(line) for line in log.read_text().splitlines() if "backend/Dockerfile.worker" in line)
    assert "XRAY_CORE_REF=v99.8.7" in worker
    assert "XRAY_IMAGE=ghcr.io/xtls/xray-core:99.8.7" in worker


def test_local_worker_binary_and_node_runtime_match_the_selected_xray_release():
    ref = json.loads((REPO / "versions.json").read_text())["xray_core_ref"]
    image = f"ghcr.io/xtls/xray-core:{ref.removeprefix('v')}"
    node = dict(
        line.split("=", 1)
        for line in (REPO / ".env.node.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    dockerfile = (REPO / "backend/Dockerfile.worker").read_text()
    default = re.search(r"^ARG XRAY_IMAGE=(\S+)$", dockerfile, re.M)
    assert default is not None
    assert node["XRAY_IMAGE"] == image
    assert default.group(1) == image
