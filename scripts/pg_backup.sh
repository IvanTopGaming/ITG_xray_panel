#!/bin/bash
set -euo pipefail
: "${POSTGRES_HOST:=postgres}"
: "${POSTGRES_USER:?}"
: "${POSTGRES_DB:?}"
: "${PGPASSWORD:?}"
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_KEEP:=14}"
case "${BACKUP_KEEP}" in
    *[!0-9]*|'') ;;
    *)
        if [ "${BACKUP_KEEP}" -lt 1 ]; then
            echo "BACKUP_KEEP must be at least 1; BACKUP_KEEP=0 makes 'tail -n +1' delete every local dump, including the one this pass is about to write, refusing" >&2
            exit 1
        fi
        ;;
esac
stamp="$(date +%Y%m%d-%H%M%S)"
out="${BACKUP_DIR}/panel-${stamp}.sql.gz"
pg_dump -h "${POSTGRES_HOST}" -U "${POSTGRES_USER}" "${POSTGRES_DB}" | gzip > "${out}.tmp"
mv "${out}.tmp" "${out}"
ls -1t "${BACKUP_DIR}"/panel-*.sql.gz | tail -n "+$((BACKUP_KEEP + 1))" | xargs -r rm -f
echo "backup written: ${out}"
