FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/metrics ./cmd/metrics && mkdir -p /out/data

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/metrics /metrics
COPY --from=build --chown=65532:65532 /out/data /data
USER nonroot:nonroot
EXPOSE 9100
ENTRYPOINT ["/metrics"]
