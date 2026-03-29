ARG CADDY_VERSION=2.11
FROM caddy:${CADDY_VERSION}-builder-alpine AS builder

RUN xcaddy build \
    --with github.com/mholt/caddy-l4

FROM caddy:${CADDY_VERSION}-alpine

COPY --from=builder /usr/bin/caddy /usr/bin/caddy

RUN apk add --no-cache gettext

ENTRYPOINT ["sh", "-c", "envsubst < /etc/caddy/caddy.json > /tmp/caddy.json && exec caddy run --config /tmp/caddy.json"]