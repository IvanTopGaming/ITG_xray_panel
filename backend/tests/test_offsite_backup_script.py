"""§: the dump leaving the machine is the only part of a backup nobody can see fail.

`pg-backup` writes into `./pg_backups` on the data tier itself, so destroying, seizing or blocking
that one VM loses the database -- which is what happened on 2026-08-23. `offsite_backup.sh` is the
pass that copies those dumps out, and every property worth having is a property of *how* it copies:

* **`copy`, never `sync`.** Local rotation keeps 90 days and the remote keeps 365. `sync` would
  delete on the far side everything local rotation has already pruned, so the remote depth would
  silently collapse to the local one and nobody would notice until a restore needed a file from
  month four.
* **A second pass must not re-upload.** The loop runs every 30 minutes against a directory holding
  up to 1080 dumps; re-uploading them all would be a permanent transfer of the whole archive every
  half hour.
* **Remote rotation is age-based and independent.** `--min-age ${OFFSITE_KEEP_DAYS}d`, scoped by an
  `--include` so the pass can never delete an object it did not put there.

rclone's `local` backend makes all three cheap to drive for real: no network, no credentials, no
mocks -- the same binary, the same two commands, a directory standing in for the remote.
"""

import os
import pathlib
import shutil
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "offsite_backup.sh"
CI = REPO / ".github" / "workflows" / "ci.yml"

DAY = 86400

needs_rclone = pytest.mark.skipif(
    shutil.which("rclone") is None,
    reason="rclone is not installed; CI installs it in the backend-tests job",
)


def _remote(config_path, destination):
    config_path.write_text("[offsite]\ntype = local\n")
    return f"offsite:{destination}"


def _dump(directory, name, content=b"dump", age_days=0):
    path = directory / name
    path.write_bytes(content)
    if age_days:
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))
    return path


def _run(source, remote, config_path, keep_days=365, expect=0):
    env = dict(os.environ)
    env.update(
        {
            "BACKUP_DIR": str(source),
            "OFFSITE_REMOTE": remote,
            "OFFSITE_KEEP_DAYS": str(keep_days),
            "OFFSITE_INTERVAL_SECONDS": "1800",
            "RCLONE_CONFIG": str(config_path),
        }
    )
    env.pop("POSTGRES_HOST", None)
    result = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert result.returncode == expect, (
        f"expected exit {expect}, got {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / "pg_backups"
    destination = tmp_path / "remote"
    source.mkdir()
    destination.mkdir()
    return source, destination, tmp_path / "rclone.conf"


@needs_rclone
def test_every_dump_reaches_the_remote(workspace):
    source, destination, config = workspace
    _dump(source, "panel-20260101-000000.sql.gz", b"one")
    _dump(source, "panel-20260101-020000.sql.gz", b"two")

    _run(source, _remote(config, destination), config)

    assert sorted(p.name for p in destination.iterdir()) == [
        "panel-20260101-000000.sql.gz",
        "panel-20260101-020000.sql.gz",
    ]


@needs_rclone
def test_a_second_pass_does_not_upload_what_is_already_there(workspace):
    source, destination, config = workspace
    _dump(source, "panel-20260101-000000.sql.gz", b"aaaa")
    remote = _remote(config, destination)
    _run(source, remote, config)

    copied = destination / "panel-20260101-000000.sql.gz"
    modified = copied.stat().st_mtime_ns
    copied.write_bytes(b"bbbb")
    os.utime(copied, ns=(modified, modified))

    _run(source, remote, config)

    assert copied.read_bytes() == b"bbbb", (
        "the second pass re-uploaded a dump it had already copied. Same size, same modtime -- "
        "rclone had every reason to skip it. Against 1080 dumps on a 30-minute loop this is the "
        "whole archive crossing the wire 48 times a day."
    )

    _dump(source, "panel-20260101-020000.sql.gz", b"three")
    _run(source, remote, config)
    assert (destination / "panel-20260101-020000.sql.gz").exists(), (
        "a new dump did not reach the remote either, so 'nothing was re-uploaded' above proves "
        "nothing -- the pass copies nothing at all."
    )


@needs_rclone
def test_the_remote_rotates_on_age_and_not_on_count(workspace):
    source, destination, config = workspace
    old = _dump(destination, "panel-20240101-000000.sql.gz", b"ancient", age_days=400)
    fresh = _dump(destination, "panel-20260101-000000.sql.gz", b"recent", age_days=1)

    _run(source, _remote(config, destination), config, keep_days=365)

    assert not old.exists(), "a dump older than OFFSITE_KEEP_DAYS survived the pass"
    assert fresh.exists(), "the pass deleted a dump inside the retention window"


@needs_rclone
def test_a_foreign_object_in_the_remote_is_never_deleted(workspace):
    source, destination, config = workspace
    stranger = _dump(destination, "someone-elses-file.tar", b"not ours", age_days=400)

    _run(source, _remote(config, destination), config, keep_days=365)

    assert stranger.exists(), (
        "the rotation deleted an object this pass did not put there. The remote may be a shared "
        "bucket or a Drive folder somebody else also writes to; both rclone calls are scoped by an "
        "--include for exactly that reason."
    )


@needs_rclone
def test_a_pass_with_no_remote_configured_fails_loudly(workspace):
    source, _destination, config = workspace
    config.write_text("[offsite]\ntype = local\n")
    _run(source, "", config, expect=1)


@needs_rclone
def test_a_pass_with_no_rclone_config_fails_loudly(workspace):
    source, destination, config = workspace
    result = _run(source, f"offsite:{destination}", config, expect=1)
    assert "rclone" in (result.stdout + result.stderr).lower()


def test_ci_installs_what_these_tests_drive():
    """The skip above must never be what CI does.

    Every assertion in this file runs the real rclone binary; on a dev checkout without it they are
    skipped, which is fine, and in CI they must not be -- a permanently-skipped execution suite is
    indistinguishable from a passing one in the summary line.
    """

    text = CI.read_text()
    assert "backend-tests:" in text, "the backend-tests job was renamed; this guard is stale"
    job = text.split("backend-tests:", 1)[1].split("\n  bot-tests:", 1)[0]
    assert "rclone" in job, (
        "the backend-tests job does not install rclone, so every execution test in this file is "
        "skipped in CI and the off-site pass is covered by nothing at all."
    )


DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pg_only = pytest.mark.skipif(
    not DSN or shutil.which("psql") is None,
    reason="DATABASE_URL_TEST unset or psql missing; CI provides both",
)


def _connection_parts():
    from urllib.parse import urlparse

    parsed = urlparse(DSN.replace("postgresql+psycopg2://", "postgresql://"))
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "postgres",
        "database": (parsed.path or "/postgres").lstrip("/"),
        "password": parsed.password or "",
    }


def _script_environment():
    """What the CONTAINER is handed: the compose file's own names, plus PGPORT.

    The script passes psql `-h`, `-U` and `-d` explicitly but never `-p`, because in the container
    the server is on the default port. Here it may not be, so the port arrives the way libpq reads
    it anyway.
    """

    parts = _connection_parts()
    return {
        "POSTGRES_HOST": parts["host"],
        "POSTGRES_USER": parts["user"],
        "POSTGRES_DB": parts["database"],
        "PGPASSWORD": parts["password"],
        "PGPORT": parts["port"],
    }


def _psql(sql, capture=False):
    """What THIS test drives psql with: libpq's own names, which are a different set.

    `POSTGRES_HOST` means nothing to psql. Setting up the fixture with the script's variable names
    would silently fall back to a unix socket as the OS user and the assertions would be reading a
    different database than the script wrote to.
    """

    parts = _connection_parts()
    result = subprocess.run(
        ["psql", "-v", "ON_ERROR_STOP=1", "-tAq", "-c", sql],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PGHOST": parts["host"],
            "PGPORT": parts["port"],
            "PGUSER": parts["user"],
            "PGDATABASE": parts["database"],
            "PGPASSWORD": parts["password"],
        },
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip() if capture else None


@needs_rclone
@pg_only
def test_a_completed_pass_records_itself_where_the_master_reads(workspace):
    """The return channel, driven end to end: rclone binary, psql binary, real table.

    Everything else about this mark is checked by reading two files and hoping they agree. This is
    the one test that puts a row in a Postgres table by running the script the container runs.
    """

    source, destination, config = workspace
    _dump(source, "panel-20260101-000000.sql.gz", b"payload")

    _psql("CREATE TABLE IF NOT EXISTS system_setting (key VARCHAR(100) PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")
    _psql("DELETE FROM system_setting WHERE key LIKE 'offsite_backup_%'")

    env = dict(os.environ)
    env.update(_script_environment())
    env.update(
        {
            "BACKUP_DIR": str(source),
            "OFFSITE_REMOTE": _remote(config, destination),
            "OFFSITE_KEEP_DAYS": "365",
            "OFFSITE_INTERVAL_SECONDS": "1800",
            "RCLONE_CONFIG": str(config),
        }
    )
    result = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    recorded = _psql(
        "SELECT value FROM system_setting WHERE key = 'offsite_backup_last_success_ms'",
        capture=True,
    )
    assert recorded, "the pass finished but wrote no success mark, so the master sees nothing"
    assert abs(int(recorded) / 1000 - time.time()) < 120
    assert (
        _psql(
            "SELECT value FROM system_setting WHERE key = 'offsite_backup_interval_seconds'",
            capture=True,
        )
        == "1800"
    )

    _psql("DELETE FROM system_setting WHERE key LIKE 'offsite_backup_%'")
