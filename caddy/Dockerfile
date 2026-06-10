ARG CADDY_VERSION=2.11
FROM caddy:${CADDY_VERSION}-builder-alpine AS builder

RUN xcaddy build \
    --with github.com/mholt/caddy-l4

WORKDIR /src/caddygen
COPY caddygen/go.mod caddygen/go.sum ./
RUN go mod download
COPY caddygen/ ./
RUN CGO_ENABLED=0 go build -o /usr/bin/caddygen .

FROM caddy:${CADDY_VERSION}-alpine

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY --from=builder /usr/bin/caddygen /usr/bin/caddygen

ENTRYPOINT ["sh", "-c", "caddygen -in /etc/caddy/routes.yaml -out /tmp/caddy.json && caddy validate --config /tmp/caddy.json && exec caddy run --config /tmp/caddy.json"]
