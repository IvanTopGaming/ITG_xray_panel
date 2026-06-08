ARG CADDY_VERSION=2.11
FROM caddy:${CADDY_VERSION}-builder-alpine AS builder

# Build Caddy with the layer4 plugin.
RUN xcaddy build \
    --with github.com/mholt/caddy-l4

# Build the caddygen config generator (Go toolchain is present in the builder image).
WORKDIR /src/caddygen
COPY caddygen/go.mod caddygen/go.sum ./
RUN go mod download
COPY caddygen/ ./
RUN CGO_ENABLED=0 go build -o /usr/bin/caddygen .

FROM caddy:${CADDY_VERSION}-alpine

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY --from=builder /usr/bin/caddygen /usr/bin/caddygen

# Generate native Caddy JSON from routes.yaml + env, validate, then run.
ENTRYPOINT ["sh", "-c", "caddygen -in /etc/caddy/routes.yaml -out /tmp/caddy.json && caddy validate --config /tmp/caddy.json && exec caddy run --config /tmp/caddy.json"]
