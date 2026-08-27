#!/bin/sh
set -eu

: "${BACKUP_DIR:=/backups}"
: "${OFFSITE_REMOTE:=}"
: "${OFFSITE_KEEP_DAYS:=365}"
: "${OFFSITE_INTERVAL_SECONDS:=1800}"
: "${RCLONE_CONFIG:=/config/rclone/rclone.conf}"
export RCLONE_CONFIG

DUMPS="panel-*.sql.gz"

if [ -z "$OFFSITE_REMOTE" ]; then
    echo "offsite: OFFSITE_REMOTE is empty, so there is nowhere to copy to" >&2
    exit 1
fi

if [ ! -f "$RCLONE_CONFIG" ]; then
    echo "offsite: no rclone configuration at ${RCLONE_CONFIG}" >&2
    exit 1
fi

rclone copy "$BACKUP_DIR" "$OFFSITE_REMOTE" --include "$DUMPS"

rclone delete "$OFFSITE_REMOTE" --include "$DUMPS" --min-age "${OFFSITE_KEEP_DAYS}d"

: "${POSTGRES_HOST:=}"
: "${POSTGRES_USER:=}"
: "${POSTGRES_DB:=}"
: "${PGPASSWORD:=}"

if [ -z "$POSTGRES_HOST" ]; then
    echo "offsite: no Postgres coordinates, so the success mark was not recorded" >&2
else
    remote_literal=$(printf '%s' "$OFFSITE_REMOTE" | sed "s/'/''/g")
    psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -q <<SQL
INSERT INTO system_setting (key, value) VALUES
  ('offsite_backup_last_success_ms', '$(( $(date +%s) * 1000 ))'),
  ('offsite_backup_interval_seconds', '${OFFSITE_INTERVAL_SECONDS}'),
  ('offsite_backup_remote', '${remote_literal}')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
SQL
fi

echo "offsite: pass complete against ${OFFSITE_REMOTE}, remote depth ${OFFSITE_KEEP_DAYS}d, next in ${OFFSITE_INTERVAL_SECONDS}s"
