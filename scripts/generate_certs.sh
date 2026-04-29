#!/bin/bash
set -e

set -a
source "$(dirname "$0")/../.env"
set +a

SHARED_DIR="$(mktemp -d)"
WORK_DIR="$(mktemp -d)"
CERT_DIR="$(pwd)/certs"
SERVER_PID=""

cleanup() {
    if [[ -n "$SERVER_PID" ]]; then
        kill "$SERVER_PID" 2>/dev/null || true
    fi
    rm -rf "$SHARED_DIR" "$WORK_DIR"
}

trap cleanup EXIT INT TERM

cd "$SHARED_DIR"
python3 -m http.server 80 &
SERVER_PID=$!
cd "$OLDPWD"

certbot certonly --webroot --webroot-path "$SHARED_DIR" \
    --renew-by-default \
    --text --agree-tos \
    --work-dir "$WORK_DIR" \
    -d "$PANEL_DOMAIN"

mkdir -p "$CERT_DIR"
cp "/etc/letsencrypt/live/$PANEL_DOMAIN/fullchain.pem" "$CERT_DIR/fullchain.pem"
cp "/etc/letsencrypt/live/$PANEL_DOMAIN/privkey.pem"   "$CERT_DIR/key.pem"
cp "/etc/letsencrypt/live/$PANEL_DOMAIN/cert.pem"      "$CERT_DIR/cert.pem"

docker compose restart caddy

kill "$SERVER_PID"
