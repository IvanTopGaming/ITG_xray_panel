FROM alpine:3.20
RUN apk add --no-cache iproute2 curl jq
COPY sync.sh /usr/local/bin/sync.sh
RUN chmod +x /usr/local/bin/sync.sh
ENTRYPOINT ["/usr/local/bin/sync.sh"]
