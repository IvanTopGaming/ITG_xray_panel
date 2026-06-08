#!/bin/bash
# Bootstrap an ITG Xray Panel production deployment.
#
# Downloads the prod docker-compose, Caddy config and .env template, then
# generates a fresh .env with strong random secrets on first run. Existing
# .env is preserved — re-running just refreshes the supporting files.
#
# Bot configuration (token, admin IDs, YooKassa keys, timezone) lives in the
# panel UI now — no YAML required. The bot service token is the only thing
# you write back to .env; everything else is set under Bot → Settings.

set -e

REPO_URL="https://raw.githubusercontent.com/IvanTopGaming/ITG_xray_panel/main"
PROJECT_DIR="ITG_xray_panel"
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
NC=$'\033[0m'

echo -e "${GREEN}ITG Xray Panel — production installer${NC}"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

download_file() {
    local remote_path=$1 local_path=$2
    mkdir -p "$(dirname "$local_path")"
    echo -n "  $local_path … "
    if curl -fsSL "$REPO_URL/$remote_path" -o "$local_path"; then
        echo -e "${GREEN}OK${NC}"
    else
        echo "FAIL"; exit 1
    fi
}

gen_secret() {
    python3 -c "import secrets; print(secrets.token_urlsafe($1))" 2>/dev/null \
        || head -c 64 /dev/urandom | base64 | tr -d '/+=\n' | head -c "$1"
}

echo "Downloading deployment files..."
download_file "docker-compose.prod.yml" "docker-compose.yml"
download_file "caddy/routes.yaml" "caddy/routes.yaml"
download_file "scripts/generate_certs.sh" "scripts/generate_certs.sh"
download_file ".env.example" ".env.example"
chmod +x scripts/generate_certs.sh

if [ -f .env ]; then
    echo -e "${YELLOW}.env exists — keeping it.${NC}"
else
    PANEL_SECRET_PATH=$(gen_secret 12)
    SECRET_KEY=$(gen_secret 48)
    PANEL_ADMIN_PASSWORD=$(gen_secret 16)
    # Derive .env from the freshly downloaded .env.example so image pins always
    # track the current release. Inject the random secrets and resolve the two
    # external images whose .env.example placeholder is a literal vX.Y.Z.
    sed \
        -e "s|change-this-secret-path|$PANEL_SECRET_PATH|" \
        -e "s|change-this-super-long-random-secret|$SECRET_KEY|" \
        -e "s|change-this-strong-password|$PANEL_ADMIN_PASSWORD|" \
        -e "s|ghcr.io/xtls/xray-core:vX.Y.Z|ghcr.io/xtls/xray-core:latest|" \
        -e "s|tecnativa/docker-socket-proxy:vX.Y.Z|tecnativa/docker-socket-proxy:latest|" \
        .env.example > .env
    echo
    echo -e "${GREEN}.env generated.${NC}"
    echo -e "${YELLOW}Admin password:${NC} $PANEL_ADMIN_PASSWORD"
    echo -e "${YELLOW}Secret path:${NC}    /$PANEL_SECRET_PATH/"
    echo "Save them somewhere safe — they're random and only shown once."
fi

cat <<EOM

${GREEN}Done.${NC} Next steps:
  1. Edit .env — set PANEL_DOMAIN, SUB_DOMAIN and CORS_ORIGINS, and point their DNS here.
  2. docker compose pull
  3. docker compose up -d backend frontend redis xray socket-proxy
       (everything except caddy and the bot)
  4. bash scripts/generate_certs.sh
       Issues the TLS cert on :80 and starts caddy. Needs certbot + DNS pointing here.
  5. Open https://<PANEL_DOMAIN>/<PANEL_SECRET_PATH>/, log in as admin.
  6. Panel → Bot → Settings:
       · Rotate BOT_SERVICE_TOKEN → copy the new value into .env
       · Set bot_token, admin Telegram IDs, timezone
       · (Optional) Set YooKassa shop_id + secret_key for paid checkout
  7. docker compose up -d bot
  8. docker compose logs -f backend bot
EOM
