#!/bin/bash
set -euo pipefail
: "${POSTGRES_HOST:=postgres}"
: "${POSTGRES_USER:?}"
: "${POSTGRES_DB:?}"
: "${PGPASSWORD:?}"
: "${BACKUP_DIR:=/backups}"
stamp="$(date +%Y%m%d-%H%M%S)"
out="${BACKUP_DIR}/panel-${stamp}.sql.gz"
pg_dump -h "${POSTGRES_HOST}" -U "${POSTGRES_USER}" "${POSTGRES_DB}" | gzip > "${out}.tmp"
mv "${out}.tmp" "${out}"
ls -1t "${BACKUP_DIR}"/panel-*.sql.gz | tail -n +15 | xargs -r rm -f
echo "backup written: ${out}"
