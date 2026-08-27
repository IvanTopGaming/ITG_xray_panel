"""The installer's off-site half, and the two ways it can be worse than nothing.

Off by default is the whole shape of it: a deployer who says no must end up with a data tier that
has no offsite container, no rclone directory and no outbound network access -- not a stopped
container and an empty config, which reads as a fault nobody caused.

The second failure is quieter. `install.sh` is what a deployer pipes into bash as root, and its data
role prints the bundle that carries every shared secret in the deployment. If a half-finished
off-site dialogue could write a config that does not work, the deployer would leave believing the
dumps were safe. The connection test is what stops that, which is why the profile is only turned on
after it passes.

The interactive dialogue itself is not covered here (spec: checked by hand). What is covered is
every path that runs without a terminal.
"""

import os
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "install.sh"


def _run(target, answers=None):
    env = dict(os.environ)
    env["DATA_HOSTNAME"] = "data.example.com"
    env.update(answers or {})
    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--role",
            "data",
            "--dir",
            str(target),
            "--source",
            str(REPO),
            "--non-interactive",
            "--no-start",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(target),
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def _env_value(path, key):
    for line in path.read_text().splitlines():
        if line.startswith(f"{key}="):
            return re.split(r"\s+#", line.partition("=")[2], maxsplit=1)[0].strip()
    return None


def test_a_data_tier_that_was_not_asked_gets_no_offsite_anything(tmp_path):
    target = tmp_path / "data"
    target.mkdir()

    _run(target)

    assert _env_value(target / ".env", "COMPOSE_PROFILES") == "", (
        "the installer turned the offsite profile on without being asked. Off-site upload gives "
        "this VM the outbound network access the rest of its configuration promises it does not "
        "have; that is a deployer's decision, not a default."
    )
    assert not (target / "rclone" / "rclone.conf").exists()


def test_the_pass_script_is_delivered_whether_or_not_it_is_switched_on(tmp_path):
    """docker-compose.postgres.yml bind-mounts it unconditionally.

    Docker does not fail on a missing bind-mount source -- it creates a DIRECTORY, and the container
    then dies on 'Is a directory' inside a loop that swallows failures. That is exactly how
    scripts/pg_backup.sh shipped once, with the shared Postgres unbacked-up and `doctor` reporting
    the container healthy.
    """

    target = tmp_path / "data"
    target.mkdir()

    _run(target)

    script = target / "scripts" / "offsite_backup.sh"
    assert script.is_file(), "install.sh does not fetch scripts/offsite_backup.sh for the data role"
    assert os.access(script, os.X_OK), "the fetched pass script is not executable"


def test_the_data_tier_now_has_an_image_pin_to_move_forward():
    body = INSTALLER.read_text()

    assert re.search(r"data\)\s*printf\s*'OFFSITE_IMAGE:offsite", body), (
        "pins_for_role still returns nothing for the data role, so `install.sh update` on that host "
        "moves no pin and the offsite image is frozen at whatever version it was installed with -- "
        "silently, because check_pins reports 'already up to date'."
    )
    assert "panel-offsite:v%s" in body, (
        "image_for has no offsite branch, so check_pins compares against an empty string"
    )
    assert 'VALUES[OFFSITE_IMAGE]="$GHCR/panel-offsite:v$(json_field "$V" offsite)"' in body


def test_the_connection_is_tested_before_the_profile_is_turned_on():
    """Spec: «Не ответило — конфиг не сохраняется, установка не идёт дальше молча.»"""

    body = INSTALLER.read_text()

    assert "rclone lsd" in body, "the installer never tests the remote it just configured"

    check = body.index("rclone lsd")
    switch = body.index('VALUES[COMPOSE_PROFILES]="offsite"')
    assert check < switch, (
        "the profile is turned on before the connection is tested. A deployer would leave believing "
        "the dumps are safe, and the only later sign would be a health line nobody has looked at yet."
    )


def test_the_passphrase_is_shown_once_and_confirmed():
    """Losing it is irreversible, and the installer already has this exact ceremony for the bundle."""

    body = INSTALLER.read_text()

    assert "OFFSITE_PASSPHRASE" in body
    assert "irreversible" in body.lower() or "cannot be recovered" in body.lower(), (
        "the installer prints an encryption passphrase without saying that losing it makes every "
        "uploaded dump permanently unreadable"
    )


@pytest.mark.parametrize(
    "trap",
    [
        "drive.file",
        "rclone obscure",
    ],
)
def test_the_installer_explains_the_traps_the_spec_names(trap):
    """Spec, *Ловушки, которые установщик обязан объяснить или обойти*.

    `drive.file` scopes a token to the OAuth client that issued it, so a deployer who later adds
    their own client_id stops seeing every backup already uploaded -- not deleted, invisible. And a
    WebDAV or SFTP `pass` is the output of `rclone obscure`, not the password: pasted as-is it
    simply never connects.
    """

    assert trap in INSTALLER.read_text(), (
        f"the installer never mentions {trap!r}, which the spec lists as a trap it must explain or work around"
    )
