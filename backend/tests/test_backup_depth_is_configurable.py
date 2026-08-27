"""The data tier's dump depth was a literal in two places and configurable in neither.

`pg_backup.sh` kept exactly 14 files (`tail -n +15`) and the compose entrypoint slept exactly 21600
seconds, so the window a restore can lose and the depth it can reach back to were both wired shut.
Ninety days at two-hour granularity is 1080 files, and the only thing standing between that and the
old numbers is that both are now read from the environment.

The rotation is driven here with a stub `pg_dump` on PATH: the script's own dump step is one
external command and stubbing it is what makes the retention arithmetic -- the part that deletes
files -- testable at all without a Postgres server.
"""

import os
import pathlib
import re
import subprocess
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "pg_backup.sh"
COMPOSE = REPO / "docker-compose.postgres.yml"


def _stub_pg_dump(directory):
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / "pg_dump"
    stub.write_text("#!/bin/sh\nprintf '%s\\n' '-- stub dump'\n")
    stub.chmod(0o755)
    return directory


def _run(backup_dir, bin_dir, keep):
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "POSTGRES_HOST": "postgres",
            "POSTGRES_USER": "panel",
            "POSTGRES_DB": "panel",
            "PGPASSWORD": "pw",
            "BACKUP_DIR": str(backup_dir),
            "BACKUP_KEEP": str(keep),
        }
    )
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result


def _aged_dumps(directory, count):
    made = []
    for index in range(count):
        path = directory / f"panel-2020010{index}-000000.sql.gz"
        path.write_bytes(b"old")
        stamp = time.time() - (count - index) * 86400
        os.utime(path, (stamp, stamp))
        made.append(path)
    return made


def test_the_rotation_keeps_exactly_backup_keep_files(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    bin_dir = _stub_pg_dump(tmp_path / "bin")

    _aged_dumps(backups, 5)
    _run(backups, bin_dir, keep=3)

    remaining = sorted(p.name for p in backups.glob("panel-*.sql.gz"))
    assert len(remaining) == 3, (
        f"BACKUP_KEEP=3 left {remaining}. The rotation is `tail -n +$((BACKUP_KEEP + 1))` -- an "
        f"off-by-one here silently keeps or drops one dump per cycle forever."
    )


def test_the_newest_dumps_are_the_ones_that_survive(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    bin_dir = _stub_pg_dump(tmp_path / "bin")

    oldest, *_ = _aged_dumps(backups, 5)
    _run(backups, bin_dir, keep=2)

    assert not oldest.exists(), "rotation deleted by name rather than by age"
    fresh = [p for p in backups.glob("panel-*.sql.gz") if p.stat().st_mtime > time.time() - 60]
    assert fresh, (
        "the run wrote no new dump at all, so 'the old one is gone' proves nothing -- the stub "
        "pg_dump never ran and this guard is vacuous."
    )


def test_the_interval_is_substituted_by_compose_and_not_handed_to_the_container():
    text = COMPOSE.read_text()

    assert "sleep ${BACKUP_INTERVAL_SECONDS:-21600}" in text, (
        "the pg-backup loop no longer reads BACKUP_INTERVAL_SECONDS, or reads it with `$$` so the "
        "container resolves it. Host-side substitution is deliberate: the value is compose "
        "plumbing, and handing it into the environment would force pg_backup.sh to name a variable "
        "it has no use for, purely to satisfy test_env_reaches_code_that_reads_it.py."
    )
    assert not re.search(r"^\s+BACKUP_INTERVAL_SECONDS:\s", text, re.M), (
        "BACKUP_INTERVAL_SECONDS is back in an environment: block. Nothing inside the image reads "
        "it, and handing it in would force pg_backup.sh to name it purely to keep "
        "test_env_reaches_code_that_reads_it.py quiet. Matched at line start on purpose: the "
        "entrypoint's own `${BACKUP_INTERVAL_SECONDS:-21600}` contains the same characters."
    )
    assert "BACKUP_KEEP: ${BACKUP_KEEP" in text, (
        "BACKUP_KEEP must stay in the environment: block -- pg_backup.sh reads it inside the container."
    )
