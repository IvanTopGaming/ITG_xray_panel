"""Four properties of the service that are invisible in its logs.

* **`./pg_backups` is mounted read-only.** The pass has no business writing into the directory
  `pg-backup` owns; a `sync` typo or an rclone flag pointed the wrong way would otherwise delete the
  local archive while reporting a successful upload.
* **`./rclone` is mounted read-WRITE, and this is the one that cannot be seen by eye.** rclone
  rewrites the refreshed OAuth token back into its own config file. On a read-only mount every
  Google Drive or Yandex.Disk deployment produces a steady trickle of write errors and keeps
  working -- until the access token expires for good, at which point the uploads stop and the only
  sign is the age of the success mark.
* **Nothing else is mounted.** The owner rejected shipping the data tier's bundle and CA off-site
  (spec, *Решения владельца*). A `.env` or `pg_certs` mount here would put them one `rclone copy`
  away from the same remote.
* **The service is profiled.** Off-site upload is opt-in, and an unconfigured data tier must have no
  such container at all rather than a stopped one -- a stopped container reads as a fault, and a
  fault nobody caused is a fault nobody investigates.
"""

import pathlib

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.postgres.yml"
EXAMPLE = REPO / ".env.data.example"

SERVICE = "offsite-backup"


def _service():
    document = yaml.safe_load(COMPOSE.read_text()) or {}
    services = document.get("services") or {}
    assert SERVICE in services, (
        f"docker-compose.postgres.yml declares no {SERVICE!r} service. Found: {sorted(services)}."
    )
    return services[SERVICE]


def _volumes():
    return {entry.split(":", 1)[0]: entry for entry in _service().get("volumes") or []}


def _raw_block():
    """The service's own lines, before YAML parsing eats the `${...}` question below."""

    lines = COMPOSE.read_text().splitlines()
    start = next((i for i, line in enumerate(lines) if line == f"  {SERVICE}:"), None)
    assert start is not None, f"no `  {SERVICE}:` line in docker-compose.postgres.yml"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i] and not lines[i].startswith("   ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_the_local_archive_is_mounted_read_only():
    entry = _volumes().get("./pg_backups")
    assert entry, f"{SERVICE} does not mount ./pg_backups at all -- it has nothing to upload"
    assert entry.endswith(":ro"), (
        f"{SERVICE} mounts ./pg_backups as {entry!r}. The pass reads that directory and must never "
        f"be able to write to it: pg-backup owns the local archive and its rotation."
    )


def test_the_rclone_config_is_mounted_writable():
    entry = _volumes().get("./rclone")
    assert entry, f"{SERVICE} does not mount ./rclone, so it has no credentials"
    assert not entry.endswith(":ro"), (
        f"{SERVICE} mounts ./rclone as {entry!r}. rclone writes the refreshed OAuth token back into "
        f"rclone.conf; read-only turns every OAuth backend into errors in the log and, once the "
        f"token finally expires, into an upload that stops with nothing else reporting a fault."
    )


def test_nothing_else_is_mounted():
    mounted = set(_volumes())
    expected = {"./pg_backups", "./rclone", "./scripts/offsite_backup.sh"}
    assert mounted == expected, (
        f"{SERVICE} mounts {sorted(mounted - expected)} beyond {sorted(expected)}. The owner "
        f"rejected shipping this tier's bundle and CA off-site; anything mounted here is one "
        f"`rclone copy` away from the remote."
    )


def test_the_service_only_exists_under_its_profile():
    assert _service().get("profiles") == ["offsite"], (
        f"{SERVICE} carries profiles={_service().get('profiles')!r}. Without the profile an "
        f"unconfigured data tier gets a container that fails every cycle; with it, it gets none."
    )


def test_no_required_variable_reference_can_refuse_an_unprofiled_up():
    """Compose interpolates the whole file before it filters by profile.

    A `${VAR:?}` anywhere inside this service therefore refuses `docker compose up -d` on a data
    tier that never asked for off-site copies -- the profile does not protect it.
    """

    assert ":?" not in _raw_block(), (
        f"the {SERVICE} block contains a `${{VAR:?}}` reference. It is interpolated whether or not "
        f"the profile is active, so it turns an opt-in feature into a start-up requirement for "
        f"every data tier."
    )


def test_the_example_carries_the_profile_switch_turned_off():
    lines = [line for line in EXAMPLE.read_text().splitlines() if line.startswith("COMPOSE_PROFILES=")]
    assert lines, (
        ".env.data.example does not define COMPOSE_PROFILES. It is the only thing that turns the "
        "offsite profile on for a plain `docker compose -f docker-compose.postgres.yml up -d`, "
        "which is the command CLAUDE.md tells a maintainer to run."
    )
    assert lines[0].strip() == "COMPOSE_PROFILES=", (
        f"the example ships {lines[0]!r}. Off-site upload is opt-in and every deployer who copies "
        f"this file must get it off."
    )


def test_the_pass_is_the_one_thing_the_container_runs():
    entrypoint = _service().get("entrypoint")
    joined = " ".join(entrypoint if isinstance(entrypoint, list) else [str(entrypoint)])
    assert "/usr/local/bin/offsite_backup.sh" in joined
    assert "|| true" in joined, (
        "the loop does not swallow a failed pass. A revoked token, a full remote or an unreachable "
        "endpoint would restart-loop the container instead of leaving the success mark to age, "
        "which is the diagnosis this whole feature is built around."
    )
    assert "sleep ${OFFSITE_INTERVAL_SECONDS" in joined, (
        "the loop does not sleep for OFFSITE_INTERVAL_SECONDS, or reads it with `$$` so the "
        "container resolves it -- the script needs the value in its environment to record it, and "
        "the loop needs it substituted host-side."
    )
