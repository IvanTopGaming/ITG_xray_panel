#!/bin/bash
# Install/update an ITG Xray Panel test deployment from the `dev` branch.
# Pulls dev-latest container images from GHCR; generates .env on first run,
# leaves it alone on subsequent runs. All bot settings (token, admins,
# proxy, panel credentials) live in the panel UI now — no YAML required.

set -e

REPO_URL="https://raw.githubusercontent.com/IvanTopGaming/ITG_xray_panel/dev"
PROJECT_DIR="ITG_xray_panel"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}ITG Xray Panel — dev deployment installer${NC}"
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
download_file "docker-compose.staging.yml" "docker-compose.yml"
download_file "caddy/routes.yaml" "caddy/routes.yaml"
download_file "scripts/generate_certs.sh" "scripts/generate_certs.sh"
download_file "scripts/generate_local_cert.sh" "scripts/generate_local_cert.sh"
download_file ".env.example" ".env.example"
chmod +x scripts/generate_certs.sh scripts/generate_local_cert.sh

if [ -f .env ]; then
    echo -e "${YELLOW}.env exists — keeping it.${NC}"
else
    PANEL_SECRET_PATH=$(gen_secret 12)
    SECRET_KEY=$(gen_secret 48)
    PANEL_ADMIN_PASSWORD=$(gen_secret 16)
    cat > .env <<EOF
XRAY_IMAGE=ghcr.io/xtls/xray-core:latest
SOCKET_PROXY_IMAGE=tecnativa/docker-socket-proxy:latest
REDIS_IMAGE=redis:alpine

PANEL_DOMAIN=panel.local
PROXY_DOMAIN=www.google.com
PANEL_SECRET_PATH=$PANEL_SECRET_PATH
SECRET_KEY=$SECRET_KEY
PANEL_ADMIN_USER=admin
PANEL_ADMIN_PASSWORD=$PANEL_ADMIN_PASSWORD
CORS_ORIGINS=https://panel.local
RATELIMIT_STORAGE_URI=redis://redis:6379/0

# Bot service token — fill after first login (panel /bot → Settings → Rotate token).
BOT_SERVICE_TOKEN=
EOF
    echo -e "${GREEN}.env generated. Admin password: $PANEL_ADMIN_PASSWORD${NC}"
fi

echo
echo -e "${GREEN}Done.${NC} Next steps:"
echo "  1. Edit .env                     — set PANEL_DOMAIN, PROXY_DOMAIN (and SUB_DOMAIN)."
echo "  2. docker compose pull"
echo "  3. Generate a TLS cert into ./certs (Caddy won't start without one):"
echo "       · local domain → bash scripts/generate_local_cert.sh   (self-signed)"
echo "       · real domain  → bash scripts/generate_certs.sh        (Let's Encrypt; needs certbot + DNS)"
echo "  4. docker compose up -d backend  — bring up the panel first."
echo "  5. Open panel → Bot → Settings   — rotate BOT_SERVICE_TOKEN, save it back into .env."
echo "  6. Set bot_token, admin_ids, telegram_proxy_url, panel_admin_user/password in the UI."
echo "  7. docker compose up -d          — bot + caddy pick up settings/certs automatically."
echo "  8. docker compose logs -f backend bot"
