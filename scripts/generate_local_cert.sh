#!/bin/bash
set -e


if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

PANEL_DOMAIN="${PANEL_DOMAIN:-panel.local}"

WORK_DIR="$(mktemp -d)"
CERT_DIR="$(pwd)/certs"

cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

echo "Generating local certificate for: $PANEL_DOMAIN"

mkdir -p "$CERT_DIR"

cat > "$WORK_DIR/openssl.cnf" <<EOF
[req]
default_bits       = 2048
distinguished_name = req_distinguished_name
req_extensions     = req_ext
x509_extensions    = v3_req
prompt             = no

[req_distinguished_name]
CN = $PANEL_DOMAIN

[req_ext]
subjectAltName = @alt_names

[v3_req]
keyUsage = keyEncipherment, dataEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = $PANEL_DOMAIN
DNS.2 = localhost
IP.1  = 127.0.0.1
EOF

openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout "$CERT_DIR/key.pem" \
    -out "$CERT_DIR/cert.pem" \
    -config "$WORK_DIR/openssl.cnf" \
    -extensions 'v3_req'

cp "$CERT_DIR/cert.pem" "$CERT_DIR/fullchain.pem"

echo "Done!"
echo "Files created in: $CERT_DIR"
echo "  - key.pem"
echo "  - cert.pem"
echo "  - fullchain.pem"