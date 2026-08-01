#!/usr/bin/env bash
#
# ITG Xray Panel installer.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/IvanTopGaming/ITG_xray_panel/main/scripts/install.sh)
#
# One host, one role, one directory. Everything this script creates lives under --dir; it installs
# no packages, writes nothing to /etc or /usr, and leaves no copy of itself behind. Its scratch
# space is a mktemp -d removed on exit, including on failure.
#
# The data tier goes first: it generates every shared secret, its own CA, and prints one bundle
# string. Every other host is given that string and derives the rest from it, so no password is
# ever typed twice and no ssh trust is needed between machines.

set -euo pipefail

REPO_SLUG="IvanTopGaming/ITG_xray_panel"
ROLES="data cron master node sub bot"

ROLE=""
DIR=""
SOURCE=""
REF="main"
BUNDLE_IN="${BUNDLE:-}"
INTERACTIVE=1
START=1

WORK=""
cleanup() { [ -n "$WORK" ] && rm -rf "$WORK"; }
trap cleanup EXIT

die() { printf '\n  error: %s\n\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }
head1() { printf '\n%s\n' "$*"; }

usage() {
    cat <<EOF
usage: install.sh [options]

  --role ROLE        data | cron | master | node | sub | bot
  --dir PATH         where the deployment lives (default: ./itg-panel)
  --bundle STRING    the string the data tier printed (not needed for --role data)
  --source PATH      install from a local checkout instead of fetching from GitHub
  --ref REF          git ref to fetch from (default: main)
  --non-interactive  ask nothing; take answers from the environment
  --no-start         write the files but do not run docker compose up
  -h, --help         this
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --role) ROLE="${2:-}"; shift 2 ;;
        --dir) DIR="${2:-}"; shift 2 ;;
        --bundle) BUNDLE_IN="${2:-}"; shift 2 ;;
        --source) SOURCE="${2:-}"; shift 2 ;;
        --ref) REF="${2:-}"; shift 2 ;;
        --non-interactive) INTERACTIVE=0; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

# ---------------------------------------------------------------------------- preflight

need() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is required and not installed. $2"
}

need openssl "Install it with your package manager (apt install openssl)."
if [ -z "$SOURCE" ]; then
    need curl "Install it with your package manager (apt install curl)."
fi
if [ "$START" -eq 1 ]; then
    need docker "See https://docs.docker.com/engine/install/ — this script deliberately does not install it for you."
    docker compose version >/dev/null 2>&1 ||
        die "the docker compose plugin is missing. Install docker-compose-plugin; 'docker-compose' (v1) is not supported."
fi

WORK="$(mktemp -d)"

# ---------------------------------------------------------------------------- helpers

gen_secret() {
    # base64url alphabet only: safe inside URIs, .env values and sed-free rendering
    openssl rand -base64 "${1:-36}" | tr -d '\n' | tr '+/' '-_' | tr -d '='
}

ask() {
    # ask VARNAME "prompt" ["default"]
    local var="$1" prompt="$2" default="${3:-}" current answer
    current="$(printf '%s' "${!var:-}")"
    if [ -n "$current" ]; then return 0; fi
    if [ "$INTERACTIVE" -eq 0 ]; then
        [ -n "$default" ] || die "--non-interactive was given but $var is unset and has no default"
        printf -v "$var" '%s' "$default"
        return 0
    fi
    if [ -n "$default" ]; then
        read -r -p "  $prompt [$default]: " answer
        answer="${answer:-$default}"
    else
        while :; do
            read -r -p "  $prompt: " answer
            [ -n "$answer" ] && break
            say "a value is required"
        done
    fi
    printf -v "$var" '%s' "$answer"
}

fetch() {
    # fetch REMOTE_PATH LOCAL_PATH
    local remote="$1" local_path="$2"
    mkdir -p "$(dirname "$local_path")"
    if [ -n "$SOURCE" ]; then
        [ -f "$SOURCE/$remote" ] || die "$SOURCE/$remote not found — is --source pointing at a checkout?"
        cp "$SOURCE/$remote" "$local_path"
    else
        curl -fsSL "https://raw.githubusercontent.com/$REPO_SLUG/$REF/$remote" -o "$local_path" ||
            die "could not download $remote from $REPO_SLUG@$REF"
    fi
}

json_field() {
    # json_field FILE KEY  — flat string values only, which is all versions.json and the bundle have
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$1" | head -1
}

declare -A VALUES=()

render_env() {
    # Rewrites the host's own .env.<role>.example, replacing the values we know and keeping
    # everything else -- the section headers and the notes explaining what breaks without each
    # variable. Done in pure bash: passwords and URIs contain characters (& | /) that turn a sed
    # replacement into a corrupted file or a silent truncation.
    local example="$1" out="$2" line key rest comment
    : > "$out"
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            rest="${BASH_REMATCH[2]}"
            comment=""
            case "$rest" in *"#"*) comment="  #${rest#*#}" ;; esac
            if [ -n "${VALUES[$key]+set}" ]; then
                printf '%s=%s%s\n' "$key" "${VALUES[$key]}" "$comment" >> "$out"
                continue
            fi
        fi
        printf '%s\n' "$line" >> "$out"
    done < "$example"
    align_env "$out"
}

align_env() {
    # Substituting values changes their width, so the trailing notes stop lining up. Re-align them
    # per block of adjacent settings, the way the examples are written. Cosmetic, and the whole
    # point of rendering from the example rather than printing a bare list of keys.
    local file="$1" tmp="$WORK/aligned" line value comment width
    local -a block=()
    : > "$tmp"

    flush_block() {
        local widest=0 entry
        for entry in "${block[@]}"; do
            value="${entry%%$'\x01'*}"
            [ "${#value}" -gt "$widest" ] && widest="${#value}"
        done
        for entry in "${block[@]}"; do
            value="${entry%%$'\x01'*}"
            comment="${entry#*$'\x01'}"
            if [ -z "$comment" ]; then
                printf '%s\n' "$value" >> "$tmp"
            else
                width=$((widest + 2))
                [ $((width + ${#comment} + 2)) -gt 100 ] && width=$((${#value} + 2))
                printf '%-*s%s\n' "$width" "$value" "$comment" >> "$tmp"
            fi
        done
        block=()
    }

    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*=[^#]*)(#.*)?$ ]]; then
            value="${BASH_REMATCH[1]}"
            comment="${BASH_REMATCH[2]:-}"
            block+=("${value%"${value##*[![:space:]]}"}"$'\x01'"$comment")
            continue
        fi
        [ "${#block[@]}" -gt 0 ] && flush_block
        printf '%s\n' "$line" >> "$tmp"
    done < "$file"
    [ "${#block[@]}" -gt 0 ] && flush_block

    mv "$tmp" "$file"
}

# ---------------------------------------------------------------------------- role

if [ -z "$ROLE" ]; then
    if [ "$INTERACTIVE" -eq 0 ]; then die "--role is required with --non-interactive"; fi
    head1 "Which role does this machine run?"
    say "data    Postgres + Redis + backups. No inbound or outbound internet. Install this first."
    say "cron    every background job; owns the shared schema, so it boots before master/sub/bot."
    say "master  admin API + admin SPA."
    say "node    Xray + node SPA. One per location."
    say "sub     subscription links and the subscription page."
    say "bot     Telegram bot, bot-api and the whole billing surface."
    printf '\n'
    ask ROLE "role"
fi
case " $ROLES " in *" $ROLE "*) ;; *) die "unknown role '$ROLE' (expected one of: $ROLES)" ;; esac

DIR="${DIR:-./itg-panel}"
mkdir -p "$DIR"
DIR="$(cd "$DIR" && pwd)"
[ -f "$DIR/.env" ] && die "$DIR/.env already exists. Move it aside first — this script will not overwrite a live deployment."

COMPOSE_FILE=""
case "$ROLE" in
    data) COMPOSE_FILE="docker-compose.postgres.yml" ;;
    cron) COMPOSE_FILE="docker-compose.cron.yml" ;;
    master) COMPOSE_FILE="docker-compose.master.yml" ;;
    node) COMPOSE_FILE="docker-compose.node.yml" ;;
    sub) COMPOSE_FILE="docker-compose.sub.yml" ;;
    bot) COMPOSE_FILE="docker-compose.bot.yml" ;;
esac

EXAMPLE_NAME=".env.${ROLE}.example"
[ "$ROLE" = "data" ] && EXAMPLE_NAME=".env.data.example"

fetch "$COMPOSE_FILE" "$DIR/$COMPOSE_FILE"
fetch "$EXAMPLE_NAME" "$WORK/example"
fetch "versions.json" "$WORK/versions.json"
case "$ROLE" in
    master|node|sub|bot) fetch "caddy/routes.yaml" "$DIR/caddy/routes.yaml" ;;
esac

# ---------------------------------------------------------------------------- image pins

V="$WORK/versions.json"
GHCR="ghcr.io/ivantopgaming"
VALUES[MASTER_IMAGE]="$GHCR/panel-master:v$(json_field "$V" master)"
VALUES[WORKER_IMAGE]="$GHCR/panel-worker:v$(json_field "$V" worker)"
VALUES[SUB_IMAGE]="$GHCR/panel-sub:v$(json_field "$V" sub)"
VALUES[BOT_API_IMAGE]="$GHCR/panel-bot-api:v$(json_field "$V" bot_api)"
VALUES[CRON_IMAGE]="$GHCR/panel-cron:v$(json_field "$V" cron)"
VALUES[BOT_IMAGE]="$GHCR/panel-bot:v$(json_field "$V" bot)"
VALUES[FRONTEND_ADMIN_IMAGE]="$GHCR/panel-frontend-admin:v$(json_field "$V" frontend_admin)"
VALUES[FRONTEND_NODE_IMAGE]="$GHCR/panel-frontend-node:v$(json_field "$V" frontend_node)"
VALUES[CADDY_IMAGE]="$GHCR/panel-caddy:v$(json_field "$V" caddy)"
VALUES[XRAY_EGRESS_IMAGE]="$GHCR/panel-egress:v$(json_field "$V" xray_egress)"

# ---------------------------------------------------------------------------- data tier

if [ "$ROLE" = "data" ]; then
    head1 "Data tier"
    say "Every other host will reach this machine by the name you give here, and the certificate"
    say "is issued for exactly that name. A private-network DNS name is the right answer."
    printf '\n'
    ask DATA_HOSTNAME "hostname other hosts will use for this machine"
    ask POSTGRES_BIND "which interface to publish Postgres and Redis on" "127.0.0.1"
    REDIS_BIND="${REDIS_BIND:-$POSTGRES_BIND}"

    POSTGRES_PASSWORD="$(gen_secret 30)"
    REDIS_PANEL_PASSWORD="$(gen_secret 30)"
    REDIS_NODE_PASSWORD="$(gen_secret 30)"
    REDIS_BOT_PASSWORD="$(gen_secret 30)"
    SECRET_KEY="$(gen_secret 48)"

    head1 "Issuing the certificate Postgres and Redis present"
    mkdir -p "$DIR/pg_certs"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$DIR/pg_certs/ca.key" -out "$DIR/pg_certs/ca.crt" \
        -subj "/CN=ITG Panel Data Tier CA" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
    openssl req -newkey rsa:2048 -sha256 -nodes \
        -keyout "$DIR/pg_certs/server.key" -out "$WORK/server.csr" \
        -subj "/CN=$DATA_HOSTNAME" 2>/dev/null
    printf 'subjectAltName=DNS:%s\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' \
        "$DATA_HOSTNAME" > "$WORK/server.ext"
    openssl x509 -req -in "$WORK/server.csr" -CA "$DIR/pg_certs/ca.crt" -CAkey "$DIR/pg_certs/ca.key" \
        -CAcreateserial -out "$DIR/pg_certs/server.crt" -days 3650 -sha256 \
        -extfile "$WORK/server.ext" 2>/dev/null

    # Postgres refuses to start on a key it considers world- or group-readable, and Redis must read
    # the same file. Both containers run as uid 999, so one owner satisfies them; the gids differ
    # (999 vs 1000), so a group-based mode would not.
    chmod 600 "$DIR/pg_certs/server.key" "$DIR/pg_certs/ca.key"
    chmod 644 "$DIR/pg_certs/server.crt" "$DIR/pg_certs/ca.crt"
    if [ "$(id -u)" = "0" ]; then
        chown 999:999 "$DIR/pg_certs/server.key" "$DIR/pg_certs/server.crt"
    else
        say "not running as root: skipping chown 999:999 on pg_certs — do it by hand or Postgres will refuse the key"
    fi

    VALUES[POSTGRES_PASSWORD]="$POSTGRES_PASSWORD"
    VALUES[REDIS_PANEL_PASSWORD]="$REDIS_PANEL_PASSWORD"
    VALUES[REDIS_NODE_PASSWORD]="$REDIS_NODE_PASSWORD"
    VALUES[REDIS_BOT_PASSWORD]="$REDIS_BOT_PASSWORD"
    VALUES[POSTGRES_BIND]="$POSTGRES_BIND"
    VALUES[REDIS_BIND]="$REDIS_BIND"

    render_env "$WORK/example" "$DIR/.env"

    CA_B64="$(base64 -w0 < "$DIR/pg_certs/ca.crt" 2>/dev/null || base64 < "$DIR/pg_certs/ca.crt" | tr -d '\n')"
    printf '{"data_hostname":"%s","postgres_user":"%s","postgres_password":"%s","postgres_db":"%s","redis_panel":"%s","redis_node":"%s","redis_bot":"%s","secret_key":"%s","ca":"%s"}' \
        "$DATA_HOSTNAME" "panel" "$POSTGRES_PASSWORD" "panel" \
        "$REDIS_PANEL_PASSWORD" "$REDIS_NODE_PASSWORD" "$REDIS_BOT_PASSWORD" \
        "$SECRET_KEY" "$CA_B64" > "$WORK/bundle.json"
    BUNDLE_OUT="$(base64 -w0 < "$WORK/bundle.json" 2>/dev/null || base64 < "$WORK/bundle.json" | tr -d '\n')"

    head1 "Done. This machine is ready to start."
    say "The line below carries every shared secret and this tier's CA. Paste it into the installer"
    say "on each of the other five hosts. Treat it as the keys to the whole deployment."
    printf '\n%s\n\n' "$BUNDLE_OUT"
else
    # ------------------------------------------------------------------------ every other host
    if [ -z "$BUNDLE_IN" ]; then
        if [ "$INTERACTIVE" -eq 0 ]; then die "role '$ROLE' needs the data tier's bundle: pass --bundle or set BUNDLE"; fi
        head1 "Paste the bundle the data tier printed"
        ask BUNDLE_IN "bundle"
    fi
    printf '%s' "$BUNDLE_IN" | base64 -d > "$WORK/bundle.json" 2>/dev/null ||
        die "that bundle is not valid base64. Copy the whole single line the data tier printed."
    grep -q '"data_hostname"' "$WORK/bundle.json" || die "that bundle does not look like one this installer produced."

    B="$WORK/bundle.json"
    DATA_HOSTNAME="$(json_field "$B" data_hostname)"
    PG_USER="$(json_field "$B" postgres_user)"
    PG_PASSWORD="$(json_field "$B" postgres_password)"
    PG_DB="$(json_field "$B" postgres_db)"
    REDIS_PANEL_PASSWORD="$(json_field "$B" redis_panel)"
    REDIS_NODE_PASSWORD="$(json_field "$B" redis_node)"
    SHARED_SECRET_KEY="$(json_field "$B" secret_key)"
    REDIS_BOT_PASSWORD="$(json_field "$B" redis_bot)"

    json_field "$B" ca | base64 -d > "$DIR/ca.crt" 2>/dev/null || die "the bundle's CA could not be decoded"
    chmod 644 "$DIR/ca.crt"

    CA_IN_CONTAINER="/etc/ssl/panel-ca.crt"
    VALUES[SECRET_KEY]="$SHARED_SECRET_KEY"
    VALUES[DATABASE_URL]="postgresql+psycopg2://${PG_USER}:${PG_PASSWORD}@${DATA_HOSTNAME}:5432/${PG_DB}?sslmode=verify-full&sslrootcert=${CA_IN_CONTAINER}"

    redis_uri() { printf 'rediss://%s:%s@%s:6379/0?ssl_ca_certs=%s' "$1" "$2" "$DATA_HOSTNAME" "$CA_IN_CONTAINER"; }
    VALUES[SHARED_REDIS_URI]="$(redis_uri panel "$REDIS_PANEL_PASSWORD")"

    case "$ROLE" in
        node) VALUES[SHARED_REDIS_URI]="$(redis_uri node "$REDIS_NODE_PASSWORD")" ;;
        bot) VALUES[BOT_SHARED_REDIS_URI]="$(redis_uri bot "$REDIS_BOT_PASSWORD")" ;;
    esac

    case "$ROLE" in
        master|node) VALUES[RATELIMIT_STORAGE_URI]="redis://redis:6379/0" ;;
        sub|bot) VALUES[RATELIMIT_STORAGE_URI]="$(redis_uri panel "$REDIS_PANEL_PASSWORD")" ;;
    esac

    head1 "This host"
    case "$ROLE" in
        master)
            ask PANEL_DOMAIN "admin panel domain for this host"
            ask SUB_DOMAIN "the subscription host's domain"
            VALUES[PANEL_DOMAIN]="$PANEL_DOMAIN"
            VALUES[SUB_DOMAIN]="$SUB_DOMAIN"
            VALUES[CORS_ORIGINS]="https://$PANEL_DOMAIN"
            VALUES[PANEL_SECRET_PATH]="$(gen_secret 12)"
            VALUES[PANEL_ADMIN_PASSWORD]="$(gen_secret 15)"
            ;;
        node)
            ask PANEL_DOMAIN "this node's own domain"
            ask PROXY_DOMAIN "decoy domain (must equal the REALITY inbound's SNI)" "www.google.com"
            ask SUB_DOMAIN "the subscription host's domain"
            VALUES[PANEL_DOMAIN]="$PANEL_DOMAIN"
            VALUES[PROXY_DOMAIN]="$PROXY_DOMAIN"
            VALUES[SUB_DOMAIN]="$SUB_DOMAIN"
            VALUES[CORS_ORIGINS]="https://$PANEL_DOMAIN"
            VALUES[PANEL_SECRET_PATH]="$(gen_secret 12)"
            VALUES[PANEL_ADMIN_PASSWORD]="$(gen_secret 15)"
            VALUES[EGRESS_INTERNAL_TOKEN]="$(gen_secret 24)"
            ;;
        sub)
            ask SUB_DOMAIN "this host's subscription domain"
            ask PANEL_DOMAIN "the master's domain"
            VALUES[SUB_DOMAIN]="$SUB_DOMAIN"
            VALUES[PANEL_DOMAIN]="$PANEL_DOMAIN"
            ;;
        bot)
            ask BOT_DOMAIN "this host's domain (serves the YooKassa webhook)"
            ask SUB_DOMAIN "the subscription host's domain"
            ask PANEL_DOMAIN "the master's domain"
            VALUES[BOT_DOMAIN]="$BOT_DOMAIN"
            VALUES[SUB_DOMAIN]="$SUB_DOMAIN"
            VALUES[PANEL_DOMAIN]="$PANEL_DOMAIN"
            ;;
    esac

    render_env "$WORK/example" "$DIR/.env"

    head1 "Done."
    case "$ROLE" in
        master|node)
            say "admin user:     admin"
            say "admin password: ${VALUES[PANEL_ADMIN_PASSWORD]}"
            say "panel URL:      https://${VALUES[PANEL_DOMAIN]}/${VALUES[PANEL_SECRET_PATH]}/"
            say "Written down only here — the .env holds them too, nothing else will show them again."
            ;;
    esac
fi

# ---------------------------------------------------------------------------- start

if [ "$START" -eq 1 ]; then
    head1 "Starting"
    ( cd "$DIR" && docker compose -f "$COMPOSE_FILE" up -d )
    say "up. Logs: docker compose -f $COMPOSE_FILE logs -f"
else
    head1 "Files written to $DIR (not started)"
    say "Start it with: cd $DIR && docker compose -f $COMPOSE_FILE up -d"
fi
