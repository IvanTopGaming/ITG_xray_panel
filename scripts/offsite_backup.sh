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

echo "offsite: pass complete against ${OFFSITE_REMOTE}, remote depth ${OFFSITE_KEEP_DAYS}d, next in ${OFFSITE_INTERVAL_SECONDS}s"
