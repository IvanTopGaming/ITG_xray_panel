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
  resp="$(curl -fsS --connect-timeout 5 --max-time 15 -H "X-Egress-Token: ${TOKEN}" "$URL" 2>/dev/null)" || { echo "egress sync: backend request failed; retrying" >&2; sleep "$INTERVAL"; continue; }
  desired="$(printf '%s' "$resp" | jq -er '
    if type == "array" and all(.[];
      (.send_through | type == "string" and test("^[0-9A-Fa-f:.]+$")) and
      (.prefix | type == "number" and . >= 0 and . <= 128 and . == floor))
    then map("\(.send_through)/\(.prefix)") | join("\n")
    else error("invalid bind plan") end' 2>/dev/null)" || {
      echo "egress sync: invalid bind plan; addresses unchanged" >&2
      sleep "$INTERVAL"
      continue
  }

  for cidr in $desired; do
    addr="${cidr%/*}"
    ip addr show dev "$IFACE" | grep -qw "$addr" || ip addr add "$cidr" dev "$IFACE" || echo "egress sync: failed to add $cidr on $IFACE" >&2
  done

  for addr in $(ip -o -4 addr show dev "$IFACE" | awk '{print $4}'); do
    iponly="${addr%/*}"
    case "$iponly" in
      172.28.0.*)
        last="${iponly##*.}"
        if [ "$last" -ge 128 ] && [ "$last" -le 254 ]; then
          echo "$desired" | grep -qw "$iponly" || ip addr del "$addr" dev "$IFACE" || echo "egress sync: failed to remove $addr on $IFACE" >&2
        fi
        ;;
    esac
  done

  sleep "$INTERVAL"
done
