# Vigil ingest service.
# Run from the ingest/ directory. Assumes `go` is on PATH and the Docker stack
# (deploy/docker-compose.yml) is up for the provisioning, smoke, and integration targets.
# Those targets source ../.env automatically for the service credentials.

ENVFILE ?= ../.env

# Source ../.env (if present) into the recipe's shell.
LOADENV = set -a; [ -f $(ENVFILE) ] && . $(ENVFILE); set +a;

.PHONY: all build fmt vet test test-integration tidy topics migrate receiver writer smoke clean

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
	$(LOADENV) go test -tags=integration ./integration/...

# Provisioning: create Redpanda topics and apply the ClickHouse schema (both idempotent).
topics:
	$(LOADENV) go run ./cmd/topics

migrate:
	$(LOADENV) go run ./cmd/migrate

# Run the binaries locally.
receiver:
	$(LOADENV) go run ./cmd/receiver

writer:
	$(LOADENV) go run ./cmd/writer

# End-to-end health check: send one trace through the running receiver+writer and verify
# it lands in ClickHouse. Requires the receiver and writer to be running.
smoke:
	$(LOADENV) go run ./cmd/smoke

clean:
	rm -rf bin dist
