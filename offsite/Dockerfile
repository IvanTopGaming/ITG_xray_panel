FROM rclone/rclone:1.68.2 AS rclone

FROM alpine:3.22

RUN apk add --no-cache ca-certificates postgresql16-client

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

CMD ["rclone", "version"]
