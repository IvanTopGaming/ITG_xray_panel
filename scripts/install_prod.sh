#!/bin/bash

set -e

REPO_URL="https://raw.githubusercontent.com/IvanTopGaming/ITG_xray_panel/main"
GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${GREEN}Starting ITG Xray Panel project initialization...${NC}"

download_file() {
    local remote_path=$1
    local local_path=$2
    
    local dir=$(dirname "$local_path")
    mkdir -p "$dir"

    echo -n "Downloading $local_path ... "

    if curl -fsSL "$REPO_URL/$remote_path" -o "$local_path"; then
        echo -e "${GREEN}OK${NC}"
    else
        echo "Error downloading!"
        exit 1
    fi
}

download_file "caddy/caddy.json" "./caddy/caddy.json"
download_file "scripts/generate_certs.sh" "./scripts/generate_certs.sh"
download_file ".env.example" "./.env.example"
download_file "bot_config.example.yaml" "./bot_config.example.yaml"

if [ -f ".env" ]; then
    echo "File .env already exists. Skipping creation."
else
    cp .env.example .env
    echo "Created .env from .env.example"
fi

if [ -f "bot_config.yaml" ]; then
    echo "File bot_config.yaml already exists. Skipping creation."
else
    cp bot_config.example.yaml bot_config.yaml
    echo "Created bot_config.yaml from bot_config.example.yaml"
fi

download_file "docker-compose.prod.yml" "docker-compose.yml"

echo -n "Setting permissions... "
chmod +x ./scripts/generate_certs.sh
echo -e "${GREEN}OK${NC}"
echo -e "${GREEN}Initialization completed successfully!${NC}"
