import os
from pathlib import Path
import subprocess

import pytest

INSTALLER = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"


def function(name):
    source = INSTALLER.read_text()
    return name + "() {" + source.split(name + "() {", 1)[1].split("\n}", 1)[0] + "\n}\n"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("abc#def", "abc#def"),
        ("'abc#def' # note", "abc#def"),
        ('"abc # def" # note', "abc # def"),
        ("abc # note", "abc"),
    ],
)
def test_env_reader_preserves_hash_in_secret(tmp_path, raw, expected):
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=" + raw + "\n")
    script = function("env_get") + '\nenv_get "$1" SECRET'
    result = subprocess.run(["bash", "-c", script, "test", str(env_file)], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == expected


def test_tcp_probe_does_not_evaluate_hostname_as_shell(tmp_path):
    marker = tmp_path / "executed"
    host = f"127.0.0.1/1; touch {marker}; #"
    script = function("tcp_open") + '\ntcp_open "$1" 1'
    subprocess.run(["bash", "-c", script, "test", host], capture_output=True, timeout=8)
    assert not marker.exists()


@pytest.mark.parametrize("secret", ["abc # def", "abc#def", "a'b # c", 'a"b # c', "a\\b # c"])
def test_env_update_roundtrips_secret_without_reviving_old_comment(tmp_path, secret):
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=old#not-a-comment\n")
    script = function("env_get") + function("env_set") + '\nenv_set "$1" SECRET "$2"\nenv_get "$1" SECRET'
    result = subprocess.run(["bash", "-c", script, "test", str(env_file), secret], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == secret
    assert "old" not in env_file.read_text()


def test_transfer_installer_refuses_http_before_curl(tmp_path):
    import base64

    token = base64.urlsafe_b64encode(b"http://master.example/path|secret").decode()
    script = (
        function("fetch_transfer_identity")
        + '\ndie() { exit 42; }\ncurl() { touch "$MARKER"; return 1; }\nfetch_transfer_identity "$1"'
    )
    marker = tmp_path / "curl-called"
    result = subprocess.run(
        ["bash", "-c", script, "test", token], env={**os.environ, "MARKER": str(marker)}, capture_output=True
    )
    assert result.returncode == 42
    assert not marker.exists()
