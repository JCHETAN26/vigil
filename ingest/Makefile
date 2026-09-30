# Vigil ingest service.
# Run from the ingest/ directory. Assumes `go` is on PATH and the Docker stack
# (deploy/docker-compose.yml) is up for the provisioning, smoke, and integration targets.
# Those targets source ../.env automatically for the service credentials.

ENVFILE ?= ../.env
COMPOSE = docker compose --env-file $(ENVFILE) -f ../deploy/docker-compose.yml

# Source ../.env (if present) into the recipe's shell.
LOADENV = set -a; [ -f $(ENVFILE) ] && . $(ENVFILE); set +a;

.PHONY: all build fmt vet test test-integration tidy topics migrate receiver writer smoke loadgen rebuild up down logs clean

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

# Bring up the whole stack (infra + ingest services), building the ingest image. The
# migrate and topics one-shot services run to completion before receiver/writer start.
up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

# Follow the receiver and writer logs.
logs:
	$(COMPOSE) logs -f receiver writer

# End-to-end health check: send one trace through the running receiver+writer and verify
# it lands in ClickHouse. Works whether they run via `make up` (containers) or `make
# receiver`/`make writer` (host).
smoke:
	$(LOADENV) go run ./cmd/smoke

# Rebuild trace_index + agent_version_stats_daily from spans FINAL (data-model §3.7). Stop the
# writer first; pass e.g. ARGS="-table agent_version_stats_daily -partition 202609".
rebuild:
	$(LOADENV) go run ./cmd/rebuild $(ARGS)

# Build the load generator (bench/ingest_load.py drives it; see cmd/loadgen).
loadgen:
	go build -o bin/loadgen ./cmd/loadgen

clean:
	rm -rf bin dist
