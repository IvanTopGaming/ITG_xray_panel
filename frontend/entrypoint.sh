#!/bin/sh

if [ -z "$PANEL_SECRET_PATH" ]; then
  echo "Error: PANEL_SECRET_PATH is not set"
  exit 1
fi

PANEL_SECRET_PATH="${PANEL_SECRET_PATH#/}"
PANEL_SECRET_PATH="${PANEL_SECRET_PATH%/}"

if [ -z "$PANEL_SECRET_PATH" ]; then
  echo "Error: PANEL_SECRET_PATH cannot be empty after trimming slashes"
  exit 1
fi

case "$PANEL_SECRET_PATH" in
  *[!A-Za-z0-9._-]*)
    echo "Error: PANEL_SECRET_PATH contains unsupported characters. Use only A-Z a-z 0-9 . _ -"
    exit 1
    ;;
esac

echo "Setting up frontend with secret path: $PANEL_SECRET_PATH"

envsubst '${PANEL_SECRET_PATH}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf

sed -i "s|<base href=\"/\"|<base href=\"/$PANEL_SECRET_PATH/\"|g" /usr/share/nginx/html/index.html

sed -i "s|window.__PANEL_BASE_URL__ = '/'|window.__PANEL_BASE_URL__ = '/$PANEL_SECRET_PATH/'|g" /usr/share/nginx/html/index.html

exec nginx -g "daemon off;"
