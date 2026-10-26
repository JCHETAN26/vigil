# Vigil

Agent reliability and evaluation platform. Traces, tests, and scores AI agents
and RAG pipelines and detects quality regressions with statistical confidence.

See [CLAUDE.md](./CLAUDE.md) for architecture and conventions.

## Try it without an API key

- **Offline, real evidence (Python only):** `make demo-evidence` recomputes the saved verdict of a
  real, exploratory HotpotQA pilot with the production gate and refuses tampered files. No
  database, no Docker, no key. → [demo/evidence/hotpotqa-pilot](./demo/evidence/hotpotqa-pilot/)
- **Interactive, synthetic examples (local stack):** `make demo` seeds a small SYNTHETIC fixture
  (invented scores and traces, no model calls) into the real Postgres/ClickHouse and starts the
  real read-only API and dashboard on loopback ports, with one `regression`, one `no_regression`
  and one `incomplete` comparison, affected cases, trace drill-down and version diff.

Prerequisites, SSH port forwarding and a 3–5 minute walkthrough: [demo/README.md](./demo/README.md).
The demo labels synthetic data in the UI (demo notice, SYNTHETIC badges, run notes); only the
pilot evidence and the measured runs below are real.

**Local only.** The API is read-only (by database role) and binds 127.0.0.1, with no
authentication; nothing is deployed and nothing here is a public service.

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
scorers, investigation agent, MCP server, Helm/Terraform, any public deployment or API
authentication.

## Results so far

Measurement results are produced by scripts in `bench/` and saved under `bench/results/`
(dated; environment and caveats in [docs/plans/status.md](./docs/plans/status.md)).

| date | what (real model calls unless noted) | result | caveat |
|---|---|---|---|
| 2026-09 | HotpotQA base100 measurement baseline `785e9e33` | 300/300 units, pass rate 0.637, $1.99 | one model, one corpus |
| 2026-10-01 | A/A test of the regression gate (same version twice, base100 × 3) | no gated regression, every metric ok | one pair |
| 2026-09-30 | τ²-bench retail dev `6b9a7dd4` | pass^1 70.0%, pass^2 60.0%, $0.86 | 10 tasks × 2 |
| 2026-10-25 | HotpotQA triage pilot `cafae810`, top_k 5 → 1 | `no_regression`; AllGoldRetrieved −0.30 (informational); $0.252951 | 20 q × 1 trial: exploratory, not equivalence ([§9](./docs/HOTPOTQA_VALIDATION.md#9-pilot-result-2026-10-25)) |
| 2026-09/10 | ingest load test (no model calls) | sustained 1k–2k spans/s with zero loss, by container limits | measured on the earlier cloud machine |

The demo's `vigil-demo-synthetic-*` runs are **not** results: their numbers are invented to
exercise the UI and the gate.

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
see [docs/REGRESSION_TRIAGE.md §6](./docs/REGRESSION_TRIAGE.md#6-running-the-api-and-dashboard-locally);
`make demo` picks its own (8091/3318). The API reads with the read-only roles created by
`deploy/create_readonly_users.sh`; without them it falls back to the admin DSN and is read-only
only by discipline.

### Stop the stack

```bash
# Stop and remove containers, keep data volumes
docker compose --env-file .env -f deploy/docker-compose.yml down

# Also delete all data (destructive: wipes traces, metadata, cache)
docker compose --env-file .env -f deploy/docker-compose.yml down -v
```
