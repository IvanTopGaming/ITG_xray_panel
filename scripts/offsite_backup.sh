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

case "${OFFSITE_KEEP_DAYS}" in
    ''|*[!0-9]*)
        echo "offsite: OFFSITE_KEEP_DAYS must be a whole number of days, at least 1 (got '${OFFSITE_KEEP_DAYS}'); refusing rather than pass a value rclone's --min-age cannot be trusted with" >&2
        exit 1
        ;;
esac

if [ "${OFFSITE_KEEP_DAYS}" -lt 1 ]; then
    echo "offsite: OFFSITE_KEEP_DAYS must be at least 1; OFFSITE_KEEP_DAYS=0 makes 'rclone delete --min-age 0d' match every dump on the remote, including the one this pass just uploaded, refusing" >&2
    exit 1
fi

case "$OFFSITE_REMOTE" in
    *,*) offsite_remote_safe="<redacted: remote carries inline rclone parameters>" ;;
    *) offsite_remote_safe="$OFFSITE_REMOTE" ;;
esac

rclone copy "$BACKUP_DIR" "$OFFSITE_REMOTE" --include "$DUMPS"

rclone delete "$OFFSITE_REMOTE" --include "$DUMPS" --min-age "${OFFSITE_KEEP_DAYS}d"

: "${POSTGRES_HOST:=}"
: "${POSTGRES_USER:=}"
: "${POSTGRES_DB:=}"
: "${PGPASSWORD:=}"

if [ -z "$POSTGRES_HOST" ]; then
    echo "offsite: no Postgres coordinates, so the success mark was not recorded" >&2
else
    remote_literal=$(printf '%s' "$offsite_remote_safe" | sed "s/'/''/g")
    interval_literal=$(printf '%s' "$OFFSITE_INTERVAL_SECONDS" | sed "s/'/''/g")
    psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -q <<SQL
INSERT INTO system_setting (key, value) VALUES
  ('offsite_backup_last_success_ms', '$(( $(date +%s) * 1000 ))'),
  ('offsite_backup_interval_seconds', '${interval_literal}'),
  ('offsite_backup_remote', '${remote_literal}')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
SQL
fi

echo "offsite: pass complete against ${offsite_remote_safe}, remote depth ${OFFSITE_KEEP_DAYS}d, next in ${OFFSITE_INTERVAL_SECONDS}s"
