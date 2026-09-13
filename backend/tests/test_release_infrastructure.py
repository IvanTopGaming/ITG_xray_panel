import gzip
import os
import pathlib
import re
import subprocess
import time

import yaml


REPO = pathlib.Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = REPO / "scripts" / "pg_backup.sh"
BACKUP_HEALTH = REPO / "scripts" / "pg_backup_health.sh"
INSTALLER = REPO / "scripts" / "install.sh"


def _write_executable(path, body):
    path.write_text(body)
    path.chmod(0o755)


def _backup_env(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "pg_dump", "#!/bin/sh\nprintf '%s\\n' 'CREATE TABLE probe(id int);'\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "POSTGRES_HOST": "postgres",
        "POSTGRES_USER": "panel",
        "POSTGRES_DB": "panel",
        "PGPASSWORD": "pw",
        "BACKUP_DIR": str(backup_dir),
        "BACKUP_KEEP": "3",
        "BACKUP_SUCCESS_FILE": str(backup_dir / ".last-success"),
    }
    return backup_dir, env


def test_successful_backup_records_a_valid_atomic_success_mark(tmp_path):
    backup_dir, env = _backup_env(tmp_path)

    result = subprocess.run(["bash", str(BACKUP_SCRIPT)], capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stderr
    mark = backup_dir / ".last-success"
    assert mark.is_file()
    assert abs(int(mark.read_text().strip()) - int(time.time())) < 10
    assert not (backup_dir / ".last-success.tmp").exists()


def test_backup_health_rejects_a_stale_mark_and_accepts_a_fresh_one(tmp_path):
    assert BACKUP_HEALTH.is_file()
    mark = tmp_path / ".last-success"
    env = {
        **os.environ,
        "BACKUP_SUCCESS_FILE": str(mark),
        "BACKUP_INTERVAL_SECONDS": "10",
        "BACKUP_STALE_AFTER_INTERVALS": "3",
    }

    mark.write_text(f"{int(time.time()) - 31}\n")
    stale = subprocess.run(["sh", str(BACKUP_HEALTH)], capture_output=True, text=True, env=env)
    assert stale.returncode != 0
    assert "stale" in stale.stderr.lower()

    mark.write_text(f"{int(time.time())}\n")
    fresh = subprocess.run(["sh", str(BACKUP_HEALTH)], capture_output=True, text=True, env=env)
    assert fresh.returncode == 0, fresh.stderr


def test_backup_health_uses_a_fresh_valid_dump_while_the_first_mark_is_being_created(tmp_path):
    dump = tmp_path / "panel-20260914-000000.sql.gz"
    with gzip.open(dump, "wb") as stream:
        stream.write(b"CREATE TABLE probe(id int);\n")
    env = {
        **os.environ,
        "BACKUP_DIR": str(tmp_path),
        "BACKUP_SUCCESS_FILE": str(tmp_path / ".last-success"),
        "BACKUP_INTERVAL_SECONDS": "10",
        "BACKUP_STALE_AFTER_INTERVALS": "3",
    }

    result = subprocess.run(["sh", str(BACKUP_HEALTH)], capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stderr


def test_pg_backup_service_exposes_the_real_backup_healthcheck():
    document = yaml.safe_load((REPO / "docker-compose.postgres.yml").read_text())
    service = document["services"]["pg-backup"]
    command = " ".join(service["healthcheck"]["test"])

    assert "/usr/local/bin/pg_backup_health.sh" in command
    assert "./scripts/pg_backup_health.sh:/usr/local/bin/pg_backup_health.sh:ro" in service["volumes"]


def _deployment(tmp_path, role="cron"):
    target = tmp_path / role
    target.mkdir()
    compose_name = "docker-compose.cron.yml" if role == "cron" else "docker-compose.postgres.yml"
    env_name = ".env.cron.example" if role == "cron" else ".env.data.example"
    (target / compose_name).write_text((REPO / compose_name).read_text())
    (target / ".env").write_text((REPO / env_name).read_text())
    return target


def _fake_docker(tmp_path, *, unhealthy=False, exited=False, fail_new_up=False):
    bin_dir = tmp_path / "docker-bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    body = f"""#!/bin/sh
printf '%s\\n' "$*" >> '{log}'
if [ "$1 $2" = "compose version" ]; then exit 0; fi
case "$*" in
  *" ps --format "*|*" ps --all --format "*)
    if [ "{int(unhealthy)}" = 1 ]; then printf '%s\\n' 'pg-backup running Up 5 minutes (unhealthy)'; elif [ "{int(exited)}" = 1 ] && printf '%s' "$*" | grep -q -- '--all'; then printf '%s\\n' 'pg-backup exited Exited (1) 5 minutes ago'; elif [ "{int(exited)}" = 0 ]; then printf '%s\\n' 'cron running Up 5 minutes (healthy)'; fi
    exit 0
    ;;
  *" pull") exit 0 ;;
  *" up -d --wait"*)
    if [ "{int(fail_new_up)}" = 1 ]; then exit 1; fi
    exit 0
    ;;
esac
exit 0
"""
    _write_executable(bin_dir / "docker", body)
    return bin_dir, log


def _installer(command, target, bin_dir):
    return subprocess.run(
        ["bash", str(INSTALLER), command, "--dir", str(target), "--source", str(REPO)],
        capture_output=True,
        text=True,
        cwd=target,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "NO_COLOR": "1"},
    )


def test_failed_update_restores_the_previous_pins_and_reports_failure(tmp_path):
    target = _deployment(tmp_path)
    old = re.sub(
        r"ghcr.io/ivantopgaming/panel-cron:v[^\s#]+",
        "ghcr.io/ivantopgaming/panel-cron:v0.0.1",
        (target / ".env").read_text(),
    )
    (target / ".env").write_text(old)
    bin_dir, log = _fake_docker(tmp_path, fail_new_up=True)

    result = _installer("update", target, bin_dir)

    assert result.returncode != 0
    assert (target / ".env").read_text() == old
    assert "up -d --wait" in log.read_text()
    assert "rollback" in (result.stdout + result.stderr).lower()


def test_doctor_returns_failure_for_an_unhealthy_container(tmp_path):
    target = _deployment(tmp_path, role="data")
    bin_dir, _ = _fake_docker(tmp_path, unhealthy=True)

    result = _installer("doctor", target, bin_dir)

    assert result.returncode != 0
    assert "unhealthy" in result.stdout.lower()


def test_doctor_includes_stopped_containers_in_its_failure_report(tmp_path):
    target = _deployment(tmp_path, role="data")
    bin_dir, _ = _fake_docker(tmp_path, exited=True)

    result = _installer("doctor", target, bin_dir)

    assert result.returncode != 0
    assert "exited" in result.stdout.lower()


def test_data_update_delivers_changed_runtime_files_even_without_an_image_bump(tmp_path):
    target = _deployment(tmp_path, role="data")
    old_compose = "services:\n  postgres: {}\n"
    (target / "docker-compose.postgres.yml").write_text(old_compose)
    scripts = target / "scripts"
    scripts.mkdir()
    _write_executable(scripts / "pg_backup.sh", "#!/bin/sh\nexit 0\n")
    _write_executable(scripts / "offsite_backup.sh", "#!/bin/sh\nexit 0\n")
    bin_dir, log = _fake_docker(tmp_path)

    result = _installer("update", target, bin_dir)

    assert result.returncode == 0, result.stderr
    assert (target / "docker-compose.postgres.yml").read_text() == (REPO / "docker-compose.postgres.yml").read_text()
    assert (scripts / "pg_backup_health.sh").read_text() == (REPO / "scripts" / "pg_backup_health.sh").read_text()
    assert os.access(scripts / "pg_backup_health.sh", os.X_OK)
    assert "up -d --wait" in log.read_text()


def test_failed_data_update_restores_the_previous_runtime_files(tmp_path):
    target = _deployment(tmp_path, role="data")
    old_compose = "services:\n  postgres: {}\n"
    (target / "docker-compose.postgres.yml").write_text(old_compose)
    scripts = target / "scripts"
    scripts.mkdir()
    old_backup = "#!/bin/sh\nprintf '%s\\n' old-backup\n"
    old_offsite = "#!/bin/sh\nprintf '%s\\n' old-offsite\n"
    _write_executable(scripts / "pg_backup.sh", old_backup)
    _write_executable(scripts / "offsite_backup.sh", old_offsite)
    bin_dir, _ = _fake_docker(tmp_path, fail_new_up=True)

    result = _installer("update", target, bin_dir)

    assert result.returncode != 0
    assert (target / "docker-compose.postgres.yml").read_text() == old_compose
    assert (scripts / "pg_backup.sh").read_text() == old_backup
    assert (scripts / "offsite_backup.sh").read_text() == old_offsite
    assert not (scripts / "pg_backup_health.sh").exists()
