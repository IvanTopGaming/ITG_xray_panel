#!/bin/sh
set -eu

: "${BACKUP_SUCCESS_FILE:=/backups/.last-success}"
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_INTERVAL_SECONDS:=21600}"
: "${BACKUP_STALE_AFTER_INTERVALS:=3}"

case "${BACKUP_INTERVAL_SECONDS}" in
    ''|*[!0-9]*) echo "backup health: invalid BACKUP_INTERVAL_SECONDS=${BACKUP_INTERVAL_SECONDS}" >&2; exit 1 ;;
esac
case "${BACKUP_STALE_AFTER_INTERVALS}" in
    ''|*[!0-9]*) echo "backup health: invalid BACKUP_STALE_AFTER_INTERVALS=${BACKUP_STALE_AFTER_INTERVALS}" >&2; exit 1 ;;
esac
if [ "${BACKUP_INTERVAL_SECONDS}" -lt 1 ] || [ "${BACKUP_STALE_AFTER_INTERVALS}" -lt 1 ]; then
    echo "backup health: intervals must be positive" >&2
    exit 1
fi
if [ -s "${BACKUP_SUCCESS_FILE}" ]; then
    last_success="$(cat "${BACKUP_SUCCESS_FILE}")"
else
    newest="$(ls -1t "${BACKUP_DIR}"/panel-*.sql.gz 2>/dev/null | head -n 1)"
    if [ -z "${newest}" ] || ! gzip -t "${newest}" 2>/dev/null; then
        echo "backup health: no successful backup recorded" >&2
        exit 1
    fi
    last_success="$(stat -c %Y "${newest}")"
fi
case "${last_success}" in
    ''|*[!0-9]*) echo "backup health: invalid success mark" >&2; exit 1 ;;
esac

now="$(date +%s)"
age=$((now - last_success))
[ "${age}" -lt 0 ] && age=0
stale_after=$((BACKUP_INTERVAL_SECONDS * BACKUP_STALE_AFTER_INTERVALS))
if [ "${age}" -gt "${stale_after}" ]; then
    echo "backup health: last successful backup is stale (${age}s > ${stale_after}s)" >&2
    exit 1
fi
