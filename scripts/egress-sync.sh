#!/usr/bin/env bash

set -Eeuo pipefail

DRY=0
DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        --dir) DIR="${2:-}"; shift 2 ;;
        *) printf 'usage: egress-sync.sh [--dry-run] [--dir PATH]\n' >&2; exit 2 ;;
    esac
done

[ -n "$DIR" ] || DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_FILE="$DIR/.env"
CONF_FILE="$DIR/egress.conf"
OWNED_FILE="$DIR/egress-owned"
CHAIN="EGRESS_SNAT"
TABLE_BASE=100
TABLE_LAST=199
PRIO_BASE=30000
PRIO_LAST=30099

log() { printf 'egress-sync: %s\n' "$*" >&2; }

conf_get() {
    [ -f "$1" ] || return 0
    sed -n "s/^$2=\([^#]*\).*/\1/p" "$1" | head -1 | sed 's/[[:space:]]*$//'
}

run() {
    if [ "$DRY" -eq 1 ]; then
        printf '+ %s\n' "$*"
    else
        "$@"
    fi
}

[ "$(id -u)" -eq 0 ] || { log "must run as root: raising an address and writing netfilter rules both need it"; exit 2; }
command -v jq >/dev/null 2>&1 || { log "jq is required"; exit 2; }
command -v iptables >/dev/null 2>&1 || { log "iptables is required"; exit 2; }

UPLINK="$(conf_get "$CONF_FILE" EGRESS_UPLINK_IFACE)"
if [ -z "$UPLINK" ]; then
    UPLINK="$(ip route show default | awk '/default/ {print $5; exit}')"
fi
[ -n "$UPLINK" ] || { log "no uplink interface found and none configured"; exit 2; }

TOKEN="$(conf_get "$ENV_FILE" EGRESS_INTERNAL_TOKEN)"
[ -n "$TOKEN" ] || { log "EGRESS_INTERNAL_TOKEN is empty in $ENV_FILE"; exit 2; }

PLAN_URL="$(conf_get "$CONF_FILE" EGRESS_PLAN_URL)"
if [ -z "$PLAN_URL" ]; then
    BACKEND_ADDR="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' panel-backend 2>/dev/null | awk '{print $1}')"
    [ -n "$BACKEND_ADDR" ] || { log "panel-backend is not running; leaving the host untouched"; exit 1; }
    PLAN_URL="http://$BACKEND_ADDR:5000/api/system/egress/host-plan"
fi

if ! PLAN="$(curl -fsS --max-time 10 -H "X-Egress-Token: $TOKEN" "$PLAN_URL")"; then
    log "the panel did not answer; leaving the host untouched"
    exit 1
fi

if ! DESIRED="$(printf '%s' "$PLAN" | jq -r '.[] | "\(.public_ip)\t\(.send_through)\t\(.gateway)"')"; then
    log "the panel's answer did not parse; leaving the host untouched"
    exit 1
fi

trap 'log "a command failed while applying the plan; the host is part-way converged, not untouched"; exit 2' ERR

current_addrs() {
    ip -4 -o addr show dev "$UPLINK" | awk '{print $4}' | cut -d/ -f1
}

owned_addrs() {
    [ -f "$OWNED_FILE" ] && cat "$OWNED_FILE" || true
}

mark_owned() {
    [ "$DRY" -eq 1 ] && return 0
    grep -qxF "$1" "$OWNED_FILE" 2>/dev/null || printf '%s\n' "$1" >> "$OWNED_FILE"
}

unmark_owned() {
    [ "$DRY" -eq 1 ] && return 0
    [ -f "$OWNED_FILE" ] || return 0
    grep -vxF "$1" "$OWNED_FILE" > "$OWNED_FILE.tmp" || true
    mv "$OWNED_FILE.tmp" "$OWNED_FILE"
}

planned_addrs() {
    printf '%s' "$DESIRED" | cut -f1
}

HAVE="$(current_addrs)"

while IFS=$'\t' read -r pub via gw; do
    [ -n "$pub" ] || continue
    if ! printf '%s\n' "$HAVE" | grep -qxF "$pub"; then
        run ip addr add "$pub/32" dev "$UPLINK"
        mark_owned "$pub"
    fi
done <<< "$DESIRED"

jump_is_first() {
    local rules
    rules="$(iptables -t nat -S POSTROUTING 2>/dev/null | grep '^-A POSTROUTING ' || true)"
    [ "${rules%%$'\n'*}" = "-A POSTROUTING -j $CHAIN" ]
}

iptables -t nat -n -L "$CHAIN" >/dev/null 2>&1 || run iptables -t nat -N "$CHAIN"

if ! iptables -t nat -C POSTROUTING -j "$CHAIN" >/dev/null 2>&1; then
    run iptables -t nat -I POSTROUTING -j "$CHAIN"
elif ! jump_is_first; then
    log "another rule was inserted ahead of $CHAIN in POSTROUTING; moving it back to the front"
    run iptables -t nat -D POSTROUTING -j "$CHAIN"
    run iptables -t nat -I POSTROUTING -j "$CHAIN"
fi

WANT_SNAT=""
while IFS=$'\t' read -r pub via gw; do
    [ -n "$pub" ] && [ -n "$via" ] || continue
    WANT_SNAT="${WANT_SNAT}-A $CHAIN -s $via/32 -j SNAT --to-source $pub"$'\n'
done <<< "$DESIRED"

HAVE_SNAT="$(iptables -t nat -S "$CHAIN" 2>/dev/null | grep -v -- "^-N $CHAIN" || true)"
[ -n "$HAVE_SNAT" ] && HAVE_SNAT="$HAVE_SNAT"$'\n'

if [ "$HAVE_SNAT" != "$WANT_SNAT" ]; then
    run iptables -t nat -F "$CHAIN"
    while IFS=$'\t' read -r pub via gw; do
        [ -n "$pub" ] && [ -n "$via" ] || continue
        run iptables -t nat -A "$CHAIN" -s "$via" -j SNAT --to-source "$pub"
    done <<< "$DESIRED"
fi

WANT_RULES=""
table="$TABLE_BASE"
prio="$PRIO_BASE"
while IFS=$'\t' read -r pub via gw; do
    [ -n "$pub" ] && [ -n "$gw" ] || continue
    if [ "$table" -gt "$TABLE_LAST" ]; then
        log "more gateways than the reserved table range holds; skipping $pub"
        continue
    fi
    WANT_RULES="${WANT_RULES}${prio}\t${pub}\t${table}\t${gw}"$'\n'
    table=$((table + 1))
    prio=$((prio + 1))
done <<< "$DESIRED"

HAVE_RULES="$(ip rule list |
    sed -n 's/^\([0-9]\{1,\}\):[[:space:]]*from \([0-9.]\{1,\}\) lookup \([0-9]\{1,\}\).*/\1\t\2\t\3/p' |
    awk -F'\t' -v lo="$PRIO_BASE" -v hi="$PRIO_LAST" '$1 >= lo && $1 <= hi')"

if [ "$HAVE_RULES" != "$(printf '%b' "$WANT_RULES" | cut -f1-3)" ]; then
    while read -r stale; do
        [ -n "$stale" ] || continue
        if ! run ip rule del priority "$stale"; then
            log "no rule left at priority $stale"
        fi
    done < <(printf '%s\n' "$HAVE_RULES" | cut -f1)

    while IFS=$'\t' read -r prio pub table gw; do
        [ -n "$pub" ] || continue
        if ! run ip route flush table "$table"; then
            log "table $table held nothing to flush"
        fi
        if ! run ip route add default via "$gw" dev "$UPLINK" table "$table"; then
            log "$pub: gateway $gw is unusable from $UPLINK; every other address keeps its routing"
            continue
        fi
        if ! run ip rule add from "$pub" table "$table" priority "$prio"; then
            log "$pub: no rule could be added at priority $prio"
        fi
    done < <(printf '%b' "$WANT_RULES")
fi

while read -r addr; do
    [ -n "$addr" ] || continue
    if printf '%s\n' "$(planned_addrs)" | grep -qxF "$addr"; then
        continue
    fi
    if ! run ip addr del "$addr/32" dev "$UPLINK"; then
        log "$addr was already gone from $UPLINK"
    fi
    unmark_owned "$addr"
done < <(owned_addrs)

exit 0
