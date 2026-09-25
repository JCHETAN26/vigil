# Multi-stage build for the ingest binaries. One image carries all four commands
# (receiver, writer, migrate, topics); each Compose service picks one via `command`.
FROM golang:1.25-alpine AS build
WORKDIR /src

# Cache module downloads.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
# Pure-Go binaries (CGO off) so they run on a minimal base.
RUN CGO_ENABLED=0 go build -trimpath -o /out/receiver ./cmd/receiver \
 && CGO_ENABLED=0 go build -trimpath -o /out/writer   ./cmd/writer \
 && CGO_ENABLED=0 go build -trimpath -o /out/migrate  ./cmd/migrate \
 && CGO_ENABLED=0 go build -trimpath -o /out/topics   ./cmd/topics

FROM alpine:3.20
# busybox wget (bundled) backs the Compose health checks; no TLS to the internal
# Redpanda/ClickHouse listeners, so no extra certs are needed.
RUN adduser -D -u 10001 vigil
COPY --from=build /out/ /usr/local/bin/
USER vigil
# Default command; each Compose service overrides it (writer/migrate/topics).
CMD ["/usr/local/bin/receiver"]
