#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

command -v certbot >/dev/null || { echo "certbot is not installed on this host." >&2; exit 1; }

set -a
source .env
set +a
: "${PANEL_DOMAIN:?set PANEL_DOMAIN in .env}"

cert_dir="$PWD/certs"
domains=(-d "$PANEL_DOMAIN")
[[ -n "${SUB_DOMAIN:-}" ]] && domains+=(-d "$SUB_DOMAIN")
[[ -n "${BOT_DOMAIN:-}" ]] && domains+=(-d "$BOT_DOMAIN")

docker compose stop caddy
trap 'docker compose up -d caddy' EXIT

certbot certonly --standalone \
    --non-interactive --agree-tos --register-unsafely-without-email \
    --expand --cert-name "$PANEL_DOMAIN" \
    "${domains[@]}"

live="/etc/letsencrypt/live/$PANEL_DOMAIN"
mkdir -p "$cert_dir"
cp -L "$live/fullchain.pem" "$cert_dir/fullchain.pem"
cp -L "$live/privkey.pem"   "$cert_dir/key.pem"

echo "Installed certificate for $PANEL_DOMAIN into $cert_dir — restarting Caddy."
