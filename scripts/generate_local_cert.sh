#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi
PANEL_DOMAIN="${PANEL_DOMAIN:-panel.local}"

cert_dir="$PWD/certs"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
mkdir -p "$cert_dir"

cat > "$work_dir/openssl.cnf" <<EOF
[req]
default_bits       = 2048
distinguished_name = req_distinguished_name
x509_extensions    = v3_req
prompt             = no

[req_distinguished_name]
CN = $PANEL_DOMAIN

[v3_req]
keyUsage = keyEncipherment, dataEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = $PANEL_DOMAIN
DNS.2 = localhost
DNS.3 = ${SUB_DOMAIN:-sub.$PANEL_DOMAIN}
IP.1  = 127.0.0.1
EOF

openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout "$cert_dir/key.pem" \
    -out "$cert_dir/fullchain.pem" \
    -config "$work_dir/openssl.cnf" \
    -extensions v3_req

echo "Self-signed certificate for $PANEL_DOMAIN written to $cert_dir (fullchain.pem, key.pem)."
