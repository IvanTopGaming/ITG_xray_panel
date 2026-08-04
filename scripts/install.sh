#!/usr/bin/env bash

set -euo pipefail

REPO_SLUG="IvanTopGaming/ITG_xray_panel"
MON_REPO_SLUG="IvanTopGaming/ITG_xray_panel_monitoring"
ROLES="data cron master node sub bot"

ROLE=""
DIR=""
SOURCE=""
REF="main"
BUNDLE_IN="${BUNDLE:-}"
MON_BUNDLE_IN="${MON_BUNDLE:-}"
MON_SOURCE=""
MONITORING=1
INTERACTIVE=1
START=1

WORK=""
cleanup() {
    spinner_stop 2>/dev/null || true
    [ -n "$WORK" ] && rm -rf "$WORK"
    printf '%b' "$SHOW_CURSOR"
}
trap cleanup EXIT

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
    C_ACCENT=$'\033[38;5;111m'; C_OK=$'\033[38;5;114m'
    C_WARN=$'\033[38;5;179m'; C_ERR=$'\033[38;5;203m'
    HIDE_CURSOR=$'\033[?25l'; SHOW_CURSOR=$'\033[?25h'
    TTY=1
else
    C_RESET=""; C_DIM=""; C_BOLD=""
    C_ACCENT=""; C_OK=""; C_WARN=""; C_ERR=""
    HIDE_CURSOR=""; SHOW_CURSOR=""
    TTY=0
fi

RULE_WIDTH=66

rule() {
    local title="${1:-}" line
    if [ -z "$title" ]; then
        printf '%b\n' "  ${C_DIM}$(printf '─%.0s' $(seq 1 $RULE_WIDTH))${C_RESET}"
        return
    fi
    line=$(printf '─%.0s' $(seq 1 $((RULE_WIDTH - ${#title} - 3))))
    printf '\n%b\n' "  ${C_DIM}──${C_RESET} ${C_BOLD}${title}${C_RESET} ${C_DIM}${line}${C_RESET}"
}

banner() {
    printf '\n'
    printf '%b\n' "  ${C_ACCENT}${C_BOLD}ITG Xray Panel${C_RESET}  ${C_DIM}· installer${C_RESET}"
    printf '%b\n' "  ${C_DIM}one host, one role, one directory${C_RESET}"
    printf '\n'
}

ok()   { printf '%b\n' "    ${C_OK}✓${C_RESET} $*"; }
info() { printf '%b\n' "    ${C_ACCENT}·${C_RESET} $*"; }
warn() { printf '%b\n' "    ${C_WARN}!${C_RESET} $*"; }
note() { printf '%b\n' "  ${C_DIM}$*${C_RESET}"; }

role_line() {
    printf '%b\n' "    ${C_BOLD}$1${C_RESET}  ${C_ACCENT}$(printf '%-11s' "$2")${C_RESET} ${C_DIM}$3${C_RESET}"
}

die() {
    spinner_stop
    printf '\n%b\n\n' "  ${C_ERR}✗${C_RESET} ${C_BOLD}$1${C_RESET}"
    [ $# -gt 1 ] && { printf '%b\n\n' "    ${C_DIM}$2${C_RESET}"; }
    exit 1
}

SPIN_PID=""
spinner_start() {
    [ "$TTY" -eq 1 ] || { printf '%b\n' "    ${C_DIM}·${C_RESET} $1…"; return; }
    printf '%b' "$HIDE_CURSOR"
    ( local frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' i=0
      while :; do
          i=$(( (i + 1) % 10 ))
          printf '\r    %b%s%b %s…' "$C_ACCENT" "${frames:$i:1}" "$C_RESET" "$1"
          sleep 0.08
      done ) &
    SPIN_PID=$!
}
spinner_stop() {
    [ -n "$SPIN_PID" ] || return 0
    kill "$SPIN_PID" 2>/dev/null || true
    wait "$SPIN_PID" 2>/dev/null || true
    SPIN_PID=""
    printf '\r\033[K'
    printf '%b' "$SHOW_CURSOR"
}

panel_top()  { printf '\n%b\n' "  ${C_ACCENT}╭$(printf '─%.0s' $(seq 1 $RULE_WIDTH))╮${C_RESET}"; }
panel_bot()  { printf '%b\n' "  ${C_ACCENT}╰$(printf '─%.0s' $(seq 1 $RULE_WIDTH))╯${C_RESET}"; }

panel_line() {
    local text="$1" pad spaces=""
    pad=$(( RULE_WIDTH - ${#text} ))
    [ "$pad" -gt 0 ] && spaces="$(printf '%*s' "$pad" '')"
    printf '%b\n' "  ${C_ACCENT}│${C_RESET}${text}${spaces}${C_ACCENT}│${C_RESET}"
}

usage() {
    cat <<EOF

  usage: install.sh [command] [options]

  commands:
    install            set this machine up as one role (default)
    doctor             check an installed host and say what is wrong
    update             move the image pins forward and restart
    reconfigure        change this host's domains, keeping every secret

  options:
    --role ROLE        data | cron | master | node | sub | bot
    --dir PATH         where the deployment lives (default: ./itg-panel)
    --bundle STRING    the string the data tier printed (not needed for --role data)
    --mon-bundle STR   the string the monitoring central printed — brings up an
                       agent next to this role once the stack is up
    --mon-source PATH  install the agent from a local monitoring checkout
    --no-monitoring    never ask about monitoring, never install the agent
    --source PATH      install from a local checkout instead of fetching from GitHub
    --ref REF          git ref to fetch from (default: main)
    --non-interactive  ask nothing; take answers from the environment
    --no-start         write the files but do not run docker compose up
    -h, --help         this

EOF
}

COMMAND="install"
COMMAND_EXPLICIT=0
case "${1:-}" in
    install|doctor|update|reconfigure) COMMAND="$1"; COMMAND_EXPLICIT=1; shift ;;
esac

while [ $# -gt 0 ]; do
    case "$1" in
        --role) ROLE="${2:-}"; shift 2 ;;
        --dir) DIR="${2:-}"; shift 2 ;;
        --bundle) BUNDLE_IN="${2:-}"; shift 2 ;;
        --mon-bundle) MON_BUNDLE_IN="${2:-}"; shift 2 ;;
        --mon-source) MON_SOURCE="${2:-}"; shift 2 ;;
        --no-monitoring) MONITORING=0; shift ;;
        --source) SOURCE="${2:-}"; shift 2 ;;
        --ref) REF="${2:-}"; shift 2 ;;
        --non-interactive) INTERACTIVE=0; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

[ "$INTERACTIVE" -eq 1 ] && banner

env_get() {
    sed -n "s/^$2=\([^#]*\).*/\1/p" "$1" | head -1 | sed 's/[[:space:]]*$//'
}

env_set() {
    local file="$1" key="$2" value="$3" line rest comment tmp
    tmp="$file.tmp"
    : > "$tmp"
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^${key}=(.*)$ ]]; then
            rest="${BASH_REMATCH[1]}"
            comment=""
            case "$rest" in *"#"*) comment="  #${rest#*#}" ;; esac
            printf '%s=%s%s\n' "$key" "$value" "$comment" >> "$tmp"
        else
            printf '%s\n' "$line" >> "$tmp"
        fi
    done < "$file"
    mv "$tmp" "$file"
}

mon_dir_for() {
    printf '%s/itg-monitoring' "$(dirname "$1")"
}

load_deployment() {
    DIR="${DIR:-.}"
    [ -d "$DIR" ] || die "$DIR does not exist" "Pass --dir pointing at the deployment directory."
    DIR="$(cd "$DIR" && pwd)"
    ENV_FILE="$DIR/.env"
    [ -f "$ENV_FILE" ] || die "no .env in $DIR" "Run the installer here first, or pass --dir."

    COMPOSE_FILE="$(cd "$DIR" && ls docker-compose.*.yml 2>/dev/null | head -1)"
    [ -n "$COMPOSE_FILE" ] || die "no docker-compose file in $DIR" "This does not look like a deployment directory."
    case "$COMPOSE_FILE" in
        docker-compose.postgres.yml) ROLE=data ;;
        docker-compose.cron.yml) ROLE=cron ;;
        docker-compose.master.yml) ROLE=master ;;
        docker-compose.node.yml) ROLE=node ;;
        docker-compose.sub.yml) ROLE=sub ;;
        docker-compose.bot.yml) ROLE=bot ;;
        *) die "unrecognised compose file $COMPOSE_FILE" ;;
    esac
}

pins_for_role() {
    case "$ROLE" in
        data) printf '' ;;
        cron) printf 'CRON_IMAGE:cron\n' ;;
        master) printf 'MASTER_IMAGE:master\nFRONTEND_ADMIN_IMAGE:frontend_admin\nCADDY_IMAGE:caddy\n' ;;
        node) printf 'WORKER_IMAGE:worker\nFRONTEND_NODE_IMAGE:frontend_node\nCADDY_IMAGE:caddy\nXRAY_EGRESS_IMAGE:xray_egress\n' ;;
        sub) printf 'SUB_IMAGE:sub\nCADDY_IMAGE:caddy\n' ;;
        bot) printf 'BOT_API_IMAGE:bot_api\nBOT_IMAGE:bot\nCADDY_IMAGE:caddy\n' ;;
    esac
}

image_for() {
    case "$2" in
        master) printf '%s/panel-master:v%s' "$GHCR" "$1" ;;
        worker) printf '%s/panel-worker:v%s' "$GHCR" "$1" ;;
        sub) printf '%s/panel-sub:v%s' "$GHCR" "$1" ;;
        bot_api) printf '%s/panel-bot-api:v%s' "$GHCR" "$1" ;;
        cron) printf '%s/panel-cron:v%s' "$GHCR" "$1" ;;
        bot) printf '%s/panel-bot:v%s' "$GHCR" "$1" ;;
        frontend_admin) printf '%s/panel-frontend-admin:v%s' "$GHCR" "$1" ;;
        frontend_node) printf '%s/panel-frontend-node:v%s' "$GHCR" "$1" ;;
        caddy) printf '%s/panel-caddy:v%s' "$GHCR" "$1" ;;
        xray_egress) printf '%s/panel-egress:v%s' "$GHCR" "$1" ;;
    esac
}

uri_host() { printf '%s' "$1" | sed -n 's|.*@\([^:/?]*\).*|\1|p'; }
uri_port() { printf '%s' "$1" | sed -n 's|.*@[^:]*:\([0-9]*\).*|\1|p'; }

tcp_open() {
    timeout 5 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null
}

GHCR="ghcr.io/ivantopgaming"

version_of() {
    case "$1" in
        docker) docker --version 2>/dev/null | sed -n 's/Docker version \([^,]*\).*/\1/p' ;;
        openssl) openssl version 2>/dev/null | awk '{print $2}' ;;
        curl) curl --version 2>/dev/null | head -1 | awk '{print $2}' ;;
    esac
}

need() {
    local cmd="$1" hint="$2" version
    if ! command -v "$cmd" >/dev/null 2>&1; then
        die "$cmd is required and not installed" "$hint"
    fi
    version="$(version_of "$cmd")"
    ok "$(printf '%-16s' "$cmd")${C_DIM}${version:-present}${C_RESET}"
}

preflight() {
    [ "$INTERACTIVE" -eq 1 ] && rule "Checking this machine"

    need openssl "Install it with your package manager, e.g. apt install openssl"
    [ -z "$SOURCE" ] && need curl "Install it with your package manager, e.g. apt install curl"
    if [ "$START" -eq 1 ]; then
        need docker "See https://docs.docker.com/engine/install/ — this installer deliberately does not install it for you."
        if docker compose version >/dev/null 2>&1; then
            ok "$(printf '%-16s' "docker compose")${C_DIM}$(docker compose version --short 2>/dev/null)${C_RESET}"
        else
            die "the docker compose plugin is missing" \
                "Install docker-compose-plugin. The old standalone 'docker-compose' (v1) is not supported."
        fi
    fi
}

WORK="$(mktemp -d)"

gen_secret() {
    openssl rand -base64 "${1:-36}" | tr -d '\n' | tr '+/' '-_' | tr -d '='
}

valid_hostname() {
    [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]
}

is_ip_literal() {
    [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || [[ "$1" == *:* ]]
}

san_for() {
    if is_ip_literal "$1"; then printf 'IP:%s' "$1"; else printf 'DNS:%s' "$1"; fi
}

ask() {
    local var="$1" prompt="$2" default="${3:-}" validator="${4:-}" answer
    if [ -n "$(printf '%s' "${!var:-}")" ]; then return 0; fi
    if [ "$INTERACTIVE" -eq 0 ]; then
        [ -n "$default" ] || die "--non-interactive was given but $var is unset and has no default"
        printf -v "$var" '%s' "$default"
        return 0
    fi
    while :; do
        if [ -n "$default" ]; then
            printf '%b' "    ${C_ACCENT}▸${C_RESET} ${prompt} ${C_DIM}[${default}]${C_RESET}: "
            read -r answer
            answer="${answer:-$default}"
        else
            printf '%b' "    ${C_ACCENT}▸${C_RESET} ${prompt}: "
            read -r answer
        fi
        [ -z "$answer" ] && { warn "a value is required"; continue; }
        if [ -n "$validator" ] && ! "$validator" "$answer"; then
            warn "that does not look like a hostname"
            continue
        fi
        break
    done
    printf -v "$var" '%s' "$answer"
}

fetch() {
    local remote="$1" local_path="$2"
    mkdir -p "$(dirname "$local_path")"
    if [ -n "$SOURCE" ]; then
        [ -f "$SOURCE/$remote" ] || die "$SOURCE/$remote not found" "Is --source pointing at a checkout of the repo?"
        cp "$SOURCE/$remote" "$local_path"
    else
        curl -fsSL "https://raw.githubusercontent.com/$REPO_SLUG/$REF/$remote" -o "$local_path" ||
            die "could not download $remote" "Tried $REPO_SLUG@$REF. Check the network, or pass --ref."
    fi
}

json_field() {
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$1" | head -1
}

declare -A VALUES=()

egress_configured() { [ -n "${DIR:-}" ] && [ -f "$DIR/egress.conf" ]; }

compose() {
    local extra=()
    if [ "${ROLE:-}" = "node" ] && egress_configured; then
        extra=(--profile egress)
    fi
    ( cd "$DIR" && docker compose -f "$COMPOSE_FILE" ${extra[@]+"${extra[@]}"} "$@" )
}

compose_show() {
    if [ "$TTY" -eq 1 ]; then
        compose "$@"
    else
        compose "$@" 2>&1 | sed 's/^/    /'
    fi
}

has_docker() { command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; }

check_pins() {
    local reported="$1" line key vkey want have stale=0
    fetch "versions.json" "$WORK/versions.json"
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        key="${line%%:*}"; vkey="${line##*:}"
        want="$(image_for "$(json_field "$WORK/versions.json" "$vkey")" "$vkey")"
        have="$(env_get "$ENV_FILE" "$key")"
        [ -n "$have" ] || continue
        if [ "$have" != "$want" ]; then
            stale=$((stale + 1))
            [ "$reported" = "report" ] && warn "$(printf '%-22s' "$key")${C_DIM}${have##*:} → ${want##*:}${C_RESET}"
            PIN_UPDATES+=("$key=$want")
        fi
    done <<< "$(pins_for_role)"
    return "$stale"
}

check_data_tier() {
    local uri host port
    uri="$(env_get "$ENV_FILE" DATABASE_URL)"
    if [ -n "$uri" ]; then
        host="$(uri_host "$uri")"; port="$(uri_port "$uri")"
        if tcp_open "$host" "${port:-5432}"; then
            ok "Postgres  ${C_DIM}${host}:${port:-5432} reachable${C_RESET}"
        else
            warn "Postgres  ${C_DIM}${host}:${port:-5432} did not answer${C_RESET}"
        fi
    fi
    uri="$(env_get "$ENV_FILE" SHARED_REDIS_URI)"
    if [ -n "$uri" ]; then
        host="$(uri_host "$uri")"; port="$(uri_port "$uri")"
        if ! tcp_open "$host" "${port:-6379}"; then
            warn "Redis     ${C_DIM}${host}:${port:-6379} did not answer${C_RESET}"
        elif [ -f "$DIR/ca.crt" ] && command -v openssl >/dev/null 2>&1; then
            if printf '' | timeout 8 openssl s_client -connect "$host:${port:-6379}" \
                 -CAfile "$DIR/ca.crt" -verify_return_error >/dev/null 2>&1; then
                ok "Redis     ${C_DIM}${host}:${port:-6379} TLS verified against ca.crt${C_RESET}"
            else
                warn "Redis     ${C_DIM}answers, but its certificate does not verify against ca.crt${C_RESET}"
            fi
        else
            ok "Redis     ${C_DIM}${host}:${port:-6379} reachable${C_RESET}"
        fi
    fi
    [ -z "$(env_get "$ENV_FILE" DATABASE_URL)$(env_get "$ENV_FILE" SHARED_REDIS_URI)" ] &&
        note "  this role holds no data-tier credentials"
    return 0
}

cmd_doctor() {
    rule "Deployment"
    ok "role      ${C_BOLD}${ROLE}${C_RESET}  ${C_DIM}${DIR}${C_RESET}"
    ok "compose   ${C_DIM}${COMPOSE_FILE}${C_RESET}"

    rule "Containers"
    if has_docker; then
        local out
        out="$(compose ps --format '{{.Service}} {{.State}} {{.Status}}' 2>/dev/null || true)"
        if [ -z "$out" ]; then
            warn "nothing is running here"
        else
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                case "$line" in
                    *running*) ok "$line" ;;
                    *) warn "$line" ;;
                esac
            done <<< "$out"
        fi
    else
        note "  docker is not available, skipping"
    fi

    rule "Data tier"
    check_data_tier

    rule "Image pins"
    PIN_UPDATES=()
    if check_pins report; then
        ok "every image is on the version this ref publishes"
    else
        note "  run: install.sh update"
    fi

    rule "Monitoring"
    check_monitoring
    printf '\n'
}

check_monitoring() {
    local mon_dir out
    mon_dir="$(mon_dir_for "$DIR")"
    if [ ! -f "$mon_dir/agent/.env" ]; then
        note "  no agent installed next to this deployment"
        return 0
    fi
    ok "agent     ${C_DIM}${mon_dir}${C_RESET}"
    has_docker || { note "  docker is not available, skipping"; return 0; }
    out="$( ( cd "$mon_dir/agent" && docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null ) || true)"
    if [ -z "$out" ]; then
        warn "the agent is installed but nothing of it runs"
        return 0
    fi
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        case "$line" in
            *running*) ok "$line" ;;
            *) warn "$line" ;;
        esac
    done <<< "$out"
}

EGRESS_UNIT=/etc/systemd/system/panel-egress-sync.service
EGRESS_TIMER=/etc/systemd/system/panel-egress-sync.timer

conf_uplink() {
    [ -f "$DIR/egress.conf" ] || return 0
    sed -n 's/^EGRESS_UPLINK_IFACE=\([^#]*\).*/\1/p' "$DIR/egress.conf" | head -1 | sed 's/[[:space:]]*$//'
}

egress_uplink() {
    local iface
    iface="$(conf_uplink)"
    [ -n "$iface" ] || iface="$(ip route show default | awk '/default/ {print $5; exit}')"
    printf '%s' "$iface"
}

egress_primary() {
    ip route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]\{1,\}\).*/\1/p' | head -1
}

egress_enable() {
    command -v jq >/dev/null 2>&1 ||
        die "jq is required to manage egress addresses" "Install it, e.g. apt install jq"
    [ "$(id -u)" = "0" ] ||
        die "managing egress addresses needs root" "Aliases and nat rules are host state."

    local iface
    iface="$(ip route show default | awk '/default/ {print $5; exit}')"
    [ -n "$iface" ] || die "no default route on this machine" "Set EGRESS_UPLINK_IFACE by hand."

    printf 'EGRESS_UPLINK_IFACE=%s\n' "$iface" > "$DIR/egress.conf"
    ok "uplink ${C_BOLD}${iface}${C_RESET} ${C_DIM}(override in $DIR/egress.conf)${C_RESET}"

    fetch "scripts/egress-sync.sh" "$DIR/egress-sync.sh"
    chmod +x "$DIR/egress-sync.sh"
    ok "egress-sync.sh"

    cat > "$EGRESS_UNIT" <<EOF
[Unit]
Description=ITG panel egress address synchroniser
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$DIR/egress-sync.sh --dir $DIR
EOF

    cat > "$EGRESS_TIMER" <<EOF
[Unit]
Description=Keep the panel's egress addresses in step

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF

    systemctl daemon-reload
    systemctl enable --now panel-egress-sync.timer >/dev/null 2>&1
    ok "panel-egress-sync.timer ${C_DIM}(every 30s, and once at boot)${C_RESET}"

    compose_show up -d xray-egress
}

egress_apply() {
    local code=0
    "$DIR/egress-sync.sh" --dir "$DIR" || code=$?
    case "$code" in
        0) ok "host is in step with the panel"; return 0 ;;
        1) warn "the synchroniser could not reach the panel — the host was left as it was" ;;
        *) warn "the synchroniser stopped part-way — the host is partly converged" ;;
    esac
    return 1
}

api_url() {
    local addr
    addr="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' panel-backend 2>/dev/null | awk '{print $1}')"
    [ -n "$addr" ] || die "panel-backend is not running" "Start the stack first: install.sh doctor"
    printf 'http://%s:5000/api' "$addr"
}

API_TOKEN=""
api_token() {
    [ -n "$API_TOKEN" ] && return 0
    local user pass body
    user="$(env_get "$ENV_FILE" PANEL_ADMIN_USER)"
    user="${user:-admin}"
    pass="$(env_get "$ENV_FILE" PANEL_ADMIN_PASSWORD)"
    while :; do
        body="$(jq -n --arg u "$user" --arg p "$pass" '{username:$u,password:$p}')"
        API_TOKEN="$(curl -fsS --max-time 10 -X POST -H 'Content-Type: application/json' \
            -d "$body" "$(api_url)/auth/login" 2>/dev/null | jq -r '.token // empty')"
        [ -n "$API_TOKEN" ] && return 0
        [ "$INTERACTIVE" -eq 1 ] || die "the admin password in .env was refused"
        warn "the panel refused the password in .env — it was changed in the UI"
        printf '%b' "    ${C_ACCENT}▸${C_RESET} admin password: "
        read -rs pass
        printf '\n'
    done
}

api_get() {
    api_token
    curl -fsS --max-time 20 -H "Authorization: Bearer $API_TOKEN" "$(api_url)$1"
}

api_post() {
    api_token
    curl -fsS --max-time 30 -X POST -H "Authorization: Bearer $API_TOKEN" \
        -H 'Content-Type: application/json' -d "$2" "$(api_url)$1"
}

api_delete() {
    api_token
    curl -fsS --max-time 30 -X DELETE -H "Authorization: Bearer $API_TOKEN" "$(api_url)$1"
}

cmd_update() {
    rule "Image pins"
    PIN_UPDATES=()
    if check_pins report; then
        ok "already up to date"
        printf '\n'
        return 0
    fi

    local entry key value
    for entry in "${PIN_UPDATES[@]}"; do
        key="${entry%%=*}"; value="${entry#*=}"
        env_set "$ENV_FILE" "$key" "$value"
    done
    ok "${#PIN_UPDATES[@]} pin(s) moved forward in .env"

    if [ "$START" -eq 0 ]; then
        rule "Not restarting"
        note "  apply with: cd $DIR && docker compose -f $COMPOSE_FILE pull && docker compose -f $COMPOSE_FILE up -d"
        printf '\n'
        return 0
    fi
    has_docker || die "docker is not available" "The pins are updated; run pull and up -d yourself."

    rule "Pulling"
    compose_show pull
    rule "Restarting"
    compose_show up -d
    printf '\n'
    ok "updated"
    printf '\n'
}

cmd_reconfigure() {
    rule "Reconfigure ${ROLE}"
    note "  Domains only. Secrets, the data-tier URIs and the admin password stay as they are."
    printf '\n'

    local changed=0
    reask() {
        local key="$1" prompt="$2" current new
        current="$(env_get "$ENV_FILE" "$key")"
        new="${!key:-}"
        if [ -z "$new" ]; then
            if [ "$INTERACTIVE" -eq 0 ]; then return 0; fi
            printf '%b' "    ${C_ACCENT}▸${C_RESET} ${prompt} ${C_DIM}[${current}]${C_RESET}: "
            read -r new
            new="${new:-$current}"
        fi
        if [ "$new" != "$current" ]; then
            env_set "$ENV_FILE" "$key" "$new"
            ok "${key} ${C_DIM}${current} → ${new}${C_RESET}"
            changed=$((changed + 1))
            case "$key" in
                PANEL_DOMAIN)
                    [ -n "$(env_get "$ENV_FILE" CORS_ORIGINS)" ] &&
                        env_set "$ENV_FILE" CORS_ORIGINS "https://$new"
                    ;;
            esac
        fi
    }

    case "$ROLE" in
        master) reask PANEL_DOMAIN "admin panel domain"; reask SUB_DOMAIN "the subscription host's domain" ;;
        node) reask PANEL_DOMAIN "this node's domain"; reask PROXY_DOMAIN "decoy domain"; reask SUB_DOMAIN "the subscription host's domain" ;;
        sub) reask SUB_DOMAIN "this host's subscription domain"; reask PANEL_DOMAIN "the master's domain" ;;
        bot) reask BOT_DOMAIN "this host's domain"; reask SUB_DOMAIN "the subscription host's domain"; reask PANEL_DOMAIN "the master's domain" ;;
        data|cron) die "role '$ROLE' has no domains to reconfigure" "Its address lives in the bundle every other host holds." ;;
    esac

    if [ "$changed" -eq 0 ]; then
        ok "nothing changed"
        printf '\n'
        return 0
    fi
    rule "Applying"
    if [ "$START" -eq 1 ] && has_docker; then
        compose_show up -d
        ok "restarted"
    else
        note "  apply with: cd $DIR && docker compose -f $COMPOSE_FILE up -d"
    fi
    printf '\n'
}

find_deployment() {
    local candidate
    for candidate in "${DIR:-}" . ./itg-panel; do
        [ -n "$candidate" ] || continue
        if [ -f "$candidate/.env" ] && ls "$candidate"/docker-compose.*.yml >/dev/null 2>&1; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

if [ "$COMMAND_EXPLICIT" -eq 0 ] && [ "$INTERACTIVE" -eq 1 ]; then
    if EXISTING="$(find_deployment)"; then
        EXISTING_ROLE="$(basename "$(ls "$EXISTING"/docker-compose.*.yml | head -1)")"
        case "$EXISTING_ROLE" in
            *postgres*) EXISTING_ROLE=data ;; *cron*) EXISTING_ROLE=cron ;;
            *master*) EXISTING_ROLE=master ;; *node*) EXISTING_ROLE=node ;;
            *sub*) EXISTING_ROLE=sub ;; *bot*) EXISTING_ROLE=bot ;;
        esac
        rule "Found a deployment here"
        ok "role ${C_BOLD}${EXISTING_ROLE}${C_RESET} ${C_DIM}in $(cd "$EXISTING" && pwd)${C_RESET}"
        printf '\n'
        role_line 1 doctor      "check it: containers, data tier, image pins"
        role_line 2 update      "move the image pins forward, pull and restart"
        role_line 3 reconfigure "change this host's domains, keep every secret"
        role_line 4 install     "set up another role, in a different directory"
        printf '\n'
        while :; do
            ask MENU_CHOICE "what would you like to do"
            case "$MENU_CHOICE" in
                1|doctor) COMMAND=doctor ;;
                2|update) COMMAND=update ;;
                3|reconfig|reconfigure) COMMAND=reconfigure ;;
                4|install) COMMAND=install ;;
                *) warn "pick 1-4"; MENU_CHOICE=""; continue ;;
            esac
            break
        done
        [ "$COMMAND" != "install" ] && DIR="$EXISTING"
    fi
fi

if [ "$COMMAND" != "install" ]; then
    load_deployment
    case "$COMMAND" in
        doctor) cmd_doctor ;;
        update) cmd_update ;;
        reconfigure) cmd_reconfigure ;;
    esac
    exit 0
fi

preflight

render_env() {
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

if [ -z "$ROLE" ]; then
    [ "$INTERACTIVE" -eq 0 ] && die "--role is required with --non-interactive"
    rule "Which role does this machine run?"
    printf '\n'
    role_line 1 data   "Postgres + Redis + backups · install this one first"
    role_line 2 cron   "every background job · owns the shared schema"
    role_line 3 master "admin API + admin SPA"
    role_line 4 node   "Xray + node SPA · one per location"
    role_line 5 sub    "subscription links and the subscription page"
    role_line 6 bot    "Telegram bot, bot-api and the billing surface"
    printf '\n'
    while :; do
        ask ROLE_CHOICE "role, by number or name"
        case "$ROLE_CHOICE" in
            1|data) ROLE=data ;; 2|cron) ROLE=cron ;; 3|master) ROLE=master ;;
            4|node) ROLE=node ;; 5|sub) ROLE=sub ;; 6|bot) ROLE=bot ;;
            *) warn "pick 1-6, or type the role name"; ROLE_CHOICE=""; continue ;;
        esac
        break
    done
fi
case " $ROLES " in *" $ROLE "*) ;; *) die "unknown role '$ROLE'" "Expected one of: $ROLES" ;; esac

DIR="${DIR:-./itg-panel}"
mkdir -p "$DIR"
DIR="$(cd "$DIR" && pwd)"
[ -f "$DIR/.env" ] && die "$DIR/.env already exists" \
    "This installer will not overwrite a live deployment. Move it aside, or pass a different --dir."

case "$ROLE" in
    data) COMPOSE_FILE="docker-compose.postgres.yml" ;;
    cron) COMPOSE_FILE="docker-compose.cron.yml" ;;
    master) COMPOSE_FILE="docker-compose.master.yml" ;;
    node) COMPOSE_FILE="docker-compose.node.yml" ;;
    sub) COMPOSE_FILE="docker-compose.sub.yml" ;;
    bot) COMPOSE_FILE="docker-compose.bot.yml" ;;
esac

rule "Fetching what this role needs"
spinner_start "reading $([ -n "$SOURCE" ] && echo "$SOURCE" || echo "$REPO_SLUG@$REF")"
fetch "$COMPOSE_FILE" "$DIR/$COMPOSE_FILE"
fetch ".env.${ROLE}.example" "$WORK/example"
fetch "versions.json" "$WORK/versions.json"
case "$ROLE" in
    master|node|sub|bot) fetch "caddy/routes.yaml" "$DIR/caddy/routes.yaml" ;;
    data) fetch "scripts/pg_backup.sh" "$DIR/scripts/pg_backup.sh"; chmod +x "$DIR/scripts/pg_backup.sh" ;;
esac
spinner_stop
ok "$COMPOSE_FILE"
case "$ROLE" in master|node|sub|bot) ok "caddy/routes.yaml" ;; esac
case "$ROLE" in data) ok "scripts/pg_backup.sh" ;; esac

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
ok "image pins from versions.json"

if [ "$ROLE" = "data" ]; then
    rule "Data tier"
    note "The address the other five hosts will use to reach this machine. It goes"
    note "into their DATABASE_URL and Redis URIs, and the certificate is issued for"
    note "exactly it — connect by any other name and TLS verification refuses."
    note "A private DNS name or this machine's private IP. Nothing public is needed."
    printf '\n'
    ask DATA_HOSTNAME "address other hosts will reach this machine at" "" valid_hostname
    ask POSTGRES_BIND "publish Postgres and Redis on which address" "127.0.0.1"
    REDIS_BIND="${REDIS_BIND:-$POSTGRES_BIND}"

    POSTGRES_PASSWORD="$(gen_secret 30)"
    REDIS_PANEL_PASSWORD="$(gen_secret 30)"
    REDIS_NODE_PASSWORD="$(gen_secret 30)"
    REDIS_BOT_PASSWORD="$(gen_secret 30)"
    REDIS_MONITORING_PASSWORD="$(gen_secret 30)"
    SECRET_KEY="$(gen_secret 48)"

    rule "Issuing this tier's certificate"
    mkdir -p "$DIR/pg_certs"

    spinner_start "generating a certificate authority"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$DIR/pg_certs/ca.key" -out "$DIR/pg_certs/ca.crt" \
        -subj "/CN=ITG Panel Data Tier CA" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
    spinner_stop
    ok "certificate authority ${C_DIM}(valid 10 years, never renewed)${C_RESET}"

    spinner_start "issuing the server certificate"
    openssl req -newkey rsa:2048 -sha256 -nodes \
        -keyout "$DIR/pg_certs/server.key" -out "$WORK/server.csr" \
        -subj "/CN=$DATA_HOSTNAME" 2>/dev/null
    printf 'subjectAltName=%s\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' \
        "$(san_for "$DATA_HOSTNAME")" > "$WORK/server.ext"
    openssl x509 -req -in "$WORK/server.csr" -CA "$DIR/pg_certs/ca.crt" -CAkey "$DIR/pg_certs/ca.key" \
        -CAcreateserial -out "$DIR/pg_certs/server.crt" -days 3650 -sha256 \
        -extfile "$WORK/server.ext" 2>/dev/null
    spinner_stop
    ok "server certificate for ${C_BOLD}${DATA_HOSTNAME}${C_RESET}"

    chmod 600 "$DIR/pg_certs/server.key" "$DIR/pg_certs/ca.key"
    chmod 644 "$DIR/pg_certs/server.crt" "$DIR/pg_certs/ca.crt"
    if [ "$(id -u)" = "0" ]; then
        chown 999:999 "$DIR/pg_certs/server.key" "$DIR/pg_certs/server.crt"
        ok "permissions ${C_DIM}(0600, owned by uid 999 — Postgres refuses anything wider)${C_RESET}"
    else
        warn "not root: could not chown pg_certs to uid 999"
        note "    Postgres will refuse the key until you do it by hand."
    fi

    VALUES[POSTGRES_PASSWORD]="$POSTGRES_PASSWORD"
    VALUES[REDIS_PANEL_PASSWORD]="$REDIS_PANEL_PASSWORD"
    VALUES[REDIS_NODE_PASSWORD]="$REDIS_NODE_PASSWORD"
    VALUES[REDIS_BOT_PASSWORD]="$REDIS_BOT_PASSWORD"
    VALUES[REDIS_MONITORING_PASSWORD]="$REDIS_MONITORING_PASSWORD"
    VALUES[POSTGRES_BIND]="$POSTGRES_BIND"
    VALUES[REDIS_BIND]="$REDIS_BIND"

    render_env "$WORK/example" "$DIR/.env"
    ok "secrets generated ${C_DIM}(1 database, 3 Redis credentials, 1 signing key)${C_RESET}"

    CA_B64="$(base64 -w0 < "$DIR/pg_certs/ca.crt" 2>/dev/null || base64 < "$DIR/pg_certs/ca.crt" | tr -d '\n')"
    printf '{"data_hostname":"%s","postgres_user":"%s","postgres_password":"%s","postgres_db":"%s","redis_panel":"%s","redis_node":"%s","redis_bot":"%s","secret_key":"%s","ca":"%s"}' \
        "$DATA_HOSTNAME" "panel" "$POSTGRES_PASSWORD" "panel" \
        "$REDIS_PANEL_PASSWORD" "$REDIS_NODE_PASSWORD" "$REDIS_BOT_PASSWORD" \
        "$SECRET_KEY" "$CA_B64" > "$WORK/bundle.json"
    BUNDLE_OUT="$(base64 -w0 < "$WORK/bundle.json" 2>/dev/null || base64 < "$WORK/bundle.json" | tr -d '\n')"

    panel_top
    panel_line ""
    panel_line "  This tier is ready. Copy the line below."
    panel_line ""
    panel_line "  It carries every shared secret and this tier's CA. Paste it"
    panel_line "  into the installer on each of the other five hosts - they"
    panel_line "  derive their own configuration from it."
    panel_line ""
    panel_line "  Treat it as the keys to the whole deployment."
    panel_line ""
    panel_bot
    printf '\n%s\n\n' "$BUNDLE_OUT"
else
    if [ -z "$BUNDLE_IN" ]; then
        [ "$INTERACTIVE" -eq 0 ] && die "role '$ROLE' needs the data tier's bundle" \
            "Pass --bundle, or set BUNDLE in the environment."
        rule "The data tier's bundle"
        note "The long line the data-tier installer printed. Paste it whole."
        printf '\n'
        ask BUNDLE_IN "bundle"
    fi
    printf '%s' "$BUNDLE_IN" | base64 -d > "$WORK/bundle.json" 2>/dev/null ||
        die "that is not a valid bundle" "Copy the whole single line the data tier printed, with nothing around it."
    grep -q '"data_hostname"' "$WORK/bundle.json" ||
        die "that bundle is not one this installer produced" "Re-run the installer on the data tier to print a fresh one."

    B="$WORK/bundle.json"
    DATA_HOSTNAME="$(json_field "$B" data_hostname)"
    PG_USER="$(json_field "$B" postgres_user)"
    PG_PASSWORD="$(json_field "$B" postgres_password)"
    PG_DB="$(json_field "$B" postgres_db)"
    REDIS_PANEL_PASSWORD="$(json_field "$B" redis_panel)"
    REDIS_NODE_PASSWORD="$(json_field "$B" redis_node)"
    REDIS_BOT_PASSWORD="$(json_field "$B" redis_bot)"
    SHARED_SECRET_KEY="$(json_field "$B" secret_key)"

    json_field "$B" ca | base64 -d > "$DIR/ca.crt" 2>/dev/null || die "the bundle's CA could not be decoded"
    chmod 644 "$DIR/ca.crt"
    ok "data tier ${C_BOLD}${DATA_HOSTNAME}${C_RESET} ${C_DIM}· credentials and CA taken from the bundle${C_RESET}"

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

    case "$ROLE" in
        master)
            rule "This host"
            ask PANEL_DOMAIN "admin panel domain for this host" "" valid_hostname
            ask SUB_DOMAIN "the subscription host's domain" "" valid_hostname
            VALUES[PANEL_DOMAIN]="$PANEL_DOMAIN"
            VALUES[SUB_DOMAIN]="$SUB_DOMAIN"
            VALUES[CORS_ORIGINS]="https://$PANEL_DOMAIN"
            VALUES[PANEL_SECRET_PATH]="$(gen_secret 12)"
            VALUES[PANEL_ADMIN_PASSWORD]="$(gen_secret 15)"
            ;;
        node)
            rule "This host"
            note "PROXY_DOMAIN is the decoy this node masquerades as. It must equal the"
            note "REALITY inbound's SNI, or every client is handed to the panel instead."
            printf '\n'
            ask PANEL_DOMAIN "this node's own domain" "" valid_hostname
            ask PROXY_DOMAIN "decoy domain" "www.google.com" valid_hostname
            ask SUB_DOMAIN "the subscription host's domain" "" valid_hostname
            VALUES[PANEL_DOMAIN]="$PANEL_DOMAIN"
            VALUES[PROXY_DOMAIN]="$PROXY_DOMAIN"
            VALUES[SUB_DOMAIN]="$SUB_DOMAIN"
            VALUES[CORS_ORIGINS]="https://$PANEL_DOMAIN"
            VALUES[PANEL_SECRET_PATH]="$(gen_secret 12)"
            VALUES[PANEL_ADMIN_PASSWORD]="$(gen_secret 15)"
            VALUES[EGRESS_INTERNAL_TOKEN]="$(gen_secret 24)"
            ;;
        sub)
            rule "This host"
            ask SUB_DOMAIN "this host's subscription domain" "" valid_hostname
            ask PANEL_DOMAIN "the master's domain" "" valid_hostname
            VALUES[SUB_DOMAIN]="$SUB_DOMAIN"
            VALUES[PANEL_DOMAIN]="$PANEL_DOMAIN"
            ;;
        bot)
            rule "This host"
            note "This host serves the YooKassa webhook and nothing else publicly."
            printf '\n'
            ask BOT_DOMAIN "this host's domain" "" valid_hostname
            ask SUB_DOMAIN "the subscription host's domain" "" valid_hostname
            ask PANEL_DOMAIN "the master's domain" "" valid_hostname
            VALUES[BOT_DOMAIN]="$BOT_DOMAIN"
            VALUES[SUB_DOMAIN]="$SUB_DOMAIN"
            VALUES[PANEL_DOMAIN]="$PANEL_DOMAIN"
            VALUES[BOT_WEBHOOK_PATH]="$(gen_secret 12)"
            ;;
    esac

    render_env "$WORK/example" "$DIR/.env"
    ok "configuration written"

    case "$ROLE" in
        bot)
            panel_top
            panel_line ""
            panel_line "  Paste the URL below into YooKassa as the notification URL."
            panel_line ""
            panel_line "  The secret segment keeps the address from being guessable -"
            panel_line "  every other path on this domain answers 404. It is not what"
            panel_line "  makes a forged notification harmless: the handler re-checks"
            panel_line "  each payment against YooKassa before granting anything."
            panel_line ""
            panel_line "  Leave it unset and payments still confirm - the poller"
            panel_line "  catches them within 30 seconds instead of instantly."
            panel_line ""
            panel_bot
            printf '\n%s\n\n' "https://${VALUES[BOT_DOMAIN]}/${VALUES[BOT_WEBHOOK_PATH]}/api/billing/yookassa/webhook"
            ;;
        master|node)
            panel_top
            panel_line ""
            panel_line "  Admin access - shown once, and also stored in .env"
            panel_line ""
            panel_line "    user      admin"
            panel_line "    password  ${VALUES[PANEL_ADMIN_PASSWORD]}"
            panel_line ""
            panel_line "    https://${VALUES[PANEL_DOMAIN]}/${VALUES[PANEL_SECRET_PATH]}/"
            panel_line ""
            panel_line "  Everything outside that path answers 404."
            panel_line ""
            panel_bot
            ;;
    esac
fi

maybe_install_monitoring() {
    [ "$MONITORING" -eq 1 ] || return 0

    if [ -z "$MON_BUNDLE_IN" ]; then
        [ "$INTERACTIVE" -eq 1 ] || return 0
        rule "Monitoring"
        note "An agent can sit next to this role and ship its metrics to a central"
        note "server: host load, containers, the backend's own /healthz — and on a"
        note "node, per-inbound Xray traffic. It publishes no port of its own."
        note "You need the bundle line the monitoring installer printed on central."
        printf '\n'
        ask MON_WANT "install the monitoring agent here? (y/N)" "N"
        case "$MON_WANT" in
            y|Y|yes|Yes) ;;
            *) return 0 ;;
        esac
        ask MON_BUNDLE_IN "monitoring bundle"
    fi

    local mon_dir
    mon_dir="$(mon_dir_for "$DIR")"

    rule "Monitoring agent"
    if [ -n "$MON_SOURCE" ]; then
        bash "$MON_SOURCE/install.sh" --role agent --panel-role "$ROLE" \
            --panel-dir "$DIR" --dir "$mon_dir" --bundle "$MON_BUNDLE_IN" \
            --source "$MON_SOURCE" --non-interactive ||
            warn "the monitoring agent did not come up — the panel itself is unaffected"
    else
        bash <(curl -fsSL "https://raw.githubusercontent.com/$MON_REPO_SLUG/main/install.sh") \
            --role agent --panel-role "$ROLE" --panel-dir "$DIR" --dir "$mon_dir" \
            --bundle "$MON_BUNDLE_IN" --non-interactive ||
            warn "the monitoring agent did not come up — the panel itself is unaffected"
    fi
}

next_steps() {
    case "$ROLE" in
        data) note "Next: run this installer on the cron host and paste the bundle above." ;;
        cron) note "Next: the master, the sub host and the bot host, in any order." ;;
        master) note "Next: bring up your nodes, then link them from Panels → Add panel." ;;
        node) note "Next: on this node open System → Link, then paste the token into the master." ;;
        sub|bot) note "Next: whichever of the six hosts you have not installed yet." ;;
    esac
}

if [ "$START" -eq 1 ]; then
    rule "Starting"
    compose_show up -d
    printf '\n'
    ok "running in ${C_BOLD}${DIR}${C_RESET}"
    note "  logs:   cd $DIR && docker compose -f $COMPOSE_FILE logs -f"
    note "  check:  install.sh doctor --dir $DIR"
    printf '\n'
    maybe_install_monitoring
    next_steps
    printf '\n'
else
    rule "Written, not started"
    ok "files in ${C_BOLD}${DIR}${C_RESET}"
    note "  start: cd $DIR && docker compose -f $COMPOSE_FILE up -d"
    printf '\n'
    next_steps
    printf '\n'
fi
