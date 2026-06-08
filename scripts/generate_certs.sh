#!/bin/bash
# Issue or renew the panel's Let's Encrypt certificate and install it where
# Caddy reads it — ./certs/{fullchain,key}.pem — as one SAN cert covering
# PANEL_DOMAIN and, when set, SUB_DOMAIN.
#
# Caddy owns :80, so certbot can't use it while the container runs. The script
# stops Caddy, issues over the standalone challenge on the freed :80, copies the
# result into ./certs, then brings Caddy back up (even if certbot fails). Re-run
# it to renew — manual, no cron. Needs certbot on the host and the domain's DNS
# already pointing here.
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

# Free :80 for the challenge, and guarantee Caddy comes back — success or not.
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
