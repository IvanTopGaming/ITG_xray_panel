#!/bin/sh
set -eu

URL="${BIND_IPS_URL:-http://backend:5000/api/system/egress/bind-ips}"
TOKEN="${EGRESS_INTERNAL_TOKEN:-}"
INTERVAL="${EGRESS_SYNC_INTERVAL:-30}"

IFACE="${XRAY_IFACE:-}"
if [ -z "$IFACE" ]; then
  IFACE=$(ip route | awk '/default/ {print $5}' | head -n1)
  IFACE="${IFACE:-eth0}"
fi

while true; do
  resp="$(curl -fsS -H "X-Egress-Token: ${TOKEN}" "$URL" 2>/dev/null)" || { sleep "$INTERVAL"; continue; }
  desired="$(echo "$resp" | jq -r '.[] | "\(.send_through)/\(.prefix)"' 2>/dev/null)" || { sleep "$INTERVAL"; continue; }

  for cidr in $desired; do
    addr="${cidr%/*}"
    ip addr show dev "$IFACE" | grep -qw "$addr" || ip addr add "$cidr" dev "$IFACE" || true
  done

  for addr in $(ip -o -4 addr show dev "$IFACE" | awk '{print $4}'); do
    iponly="${addr%/*}"
    case "$iponly" in
      172.28.0.*)
        last="${iponly##*.}"
        if [ "$last" -ge 128 ] && [ "$last" -le 254 ]; then
          echo "$desired" | grep -qw "$iponly" || ip addr del "$addr" dev "$IFACE" || true
        fi
        ;;
    esac
  done

  sleep "$INTERVAL"
done
