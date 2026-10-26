# Vigil

Agent reliability and evaluation platform. Traces, tests, and scores AI agents
and RAG pipelines and detects quality regressions with statistical confidence.

See [CLAUDE.md](./CLAUDE.md) for architecture and conventions.

## What exists today

- **Tracing:** Python SDK (OpenTelemetry) → Go OTLP receiver (gRPC + HTTP) → Kafka-API queue
  → writer → ClickHouse.
- **Evaluation:** `python -m engine run` executes agents (hello_agent, HotpotQA RAG,
  τ²-bench retail) over materialized suites with per-run cost budgets, and stores per-trial
  results in Postgres.
- **Regression triage:** `python -m engine regress` and the read-only dashboard API share one
  gate (`engine/engine/gate.py`), so both return the same verdict for the same pair. The
  dashboard compare page lists affected cases with baseline and candidate trace links and a
  version config diff. `--save` exports an evidence bundle that
  `bench/regression_verdict.py reproduce` re-checks offline. Walkthrough:
  [docs/REGRESSION_TRIAGE.md](./docs/REGRESSION_TRIAGE.md).
- **Tests:** `make test-all`. See [docs/TESTING.md](./docs/TESTING.md) for each gate and
  what it needs.

**Planned, not implemented:** TypeScript SDK (`sdk-ts/` is a placeholder), LLM-as-judge
scorers, investigation agent, MCP server, Helm/Terraform.

Measurement results are produced by scripts in `bench/` and saved under `bench/results/`.

## Local infrastructure

The backing services run in Docker: **Redpanda** (Kafka-compatible queue),
**ClickHouse** (traces), **Postgres** (metadata, eval results), and **Redis**
(dev-only LLM cache). All published ports are bound to `127.0.0.1`, so nothing is
exposed to your local network.

### Prerequisites

- Docker Engine with the Compose v2 plugin (`docker compose version`)

### Start the stack

```bash
# One-time: create your local env file and set passwords
cp .env.example .env
$EDITOR .env

# Bring everything up (run from the repo root)
docker compose --env-file .env -f deploy/docker-compose.yml up -d
```

### Check health

```bash
docker compose --env-file .env -f deploy/docker-compose.yml ps
```

Wait until every service shows `(healthy)`. Follow logs with:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml logs -f
```

### Endpoints (host)

| Service    | Endpoint                     | Notes                              |
|------------|------------------------------|------------------------------------|
| Redpanda   | `localhost:19092`            | Kafka API (external listener)      |
| Redpanda   | `localhost:18081`            | Schema Registry                    |
| Redpanda   | `localhost:18082`            | HTTP Proxy (pandaproxy)            |
| Redpanda   | `localhost:9644`             | Admin API                          |
| ClickHouse | `localhost:8123` / `:9000`   | HTTP / native TCP                  |
| Postgres   | `localhost:5432`             | user/db from `.env`                |
| Redis      | `localhost:6379`             | password from `.env`               |

Inside the Compose network, containers reach Redpanda at `redpanda:9092`.

The dashboard API (`127.0.0.1:8080`) and the Next.js dev server (`127.0.0.1:3000`) are not
part of Compose; `make dev` starts both on these defaults. For an isolated copy on other ports,
see [docs/REGRESSION_TRIAGE.md §6](./docs/REGRESSION_TRIAGE.md#6-running-the-api-and-dashboard-locally).

### Stop the stack

```bash
# Stop and remove containers, keep data volumes
docker compose --env-file .env -f deploy/docker-compose.yml down

# Also delete all data (destructive: wipes traces, metadata, cache)
docker compose --env-file .env -f deploy/docker-compose.yml down -v
```
