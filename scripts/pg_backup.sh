#!/bin/bash
set -euo pipefail
: "${POSTGRES_HOST:=postgres}"
: "${POSTGRES_USER:?}"
: "${POSTGRES_DB:?}"
: "${PGPASSWORD:?}"
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_KEEP:=14}"
: "${BACKUP_SUCCESS_FILE:=${BACKUP_DIR}/.last-success}"
case "${BACKUP_KEEP}" in
    ''|*[!0-9]*)
        echo "BACKUP_KEEP must be a whole number of dumps, at least 1 (got '${BACKUP_KEEP}'); refusing rather than pass a value the rotation's arithmetic cannot be trusted with" >&2
        exit 1
        ;;
esac
if [ "${BACKUP_KEEP}" -lt 1 ]; then
    echo "BACKUP_KEEP must be at least 1; BACKUP_KEEP=0 makes 'tail -n +1' delete every local dump, including the one this pass is about to write, refusing" >&2
    exit 1
fi
stamp="$(date +%Y%m%d-%H%M%S)"
out="${BACKUP_DIR}/panel-${stamp}.sql.gz"
dump_tmp="${out}.tmp"
mark_tmp="${BACKUP_SUCCESS_FILE}.tmp"
cleanup() {
    rm -f "${dump_tmp}" "${mark_tmp}"
}
trap cleanup EXIT
pg_dump -h "${POSTGRES_HOST}" -U "${POSTGRES_USER}" "${POSTGRES_DB}" | gzip > "${dump_tmp}"
gzip -t "${dump_tmp}"
mv "${dump_tmp}" "${out}"
ls -1t "${BACKUP_DIR}"/panel-*.sql.gz | tail -n "+$((BACKUP_KEEP + 1))" | xargs -r rm -f
printf '%s\n' "$(date +%s)" > "${mark_tmp}"
mv "${mark_tmp}" "${BACKUP_SUCCESS_FILE}"
trap - EXIT
echo "backup written: ${out}"
