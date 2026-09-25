# Vigil ingest service.
# Run from the ingest/ directory. Assumes `go` is on PATH and the Docker stack
# (deploy/docker-compose.yml) is up for the provisioning and integration targets.
# Env vars are read from the process environment; source ../.env first, e.g.:
#   set -a && . ../.env && set +a && make migrate

.PHONY: all build fmt vet test test-integration tidy topics migrate receiver writer clean

all: build

tidy:
	go mod tidy

build:
	go build ./...

fmt:
	gofmt -w .

vet:
	go vet ./...

# Unit tests only (no external services).
test:
	go test ./...

# Integration tests against the running Docker stack (build-tagged).
test-integration:
	go test -tags=integration ./integration/...

# Provisioning: create Redpanda topics and apply the ClickHouse schema (both idempotent).
topics:
	go run ./cmd/topics

migrate:
	go run ./cmd/migrate

# Run the binaries locally.
receiver:
	go run ./cmd/receiver

writer:
	go run ./cmd/writer

clean:
	rm -rf bin dist
