# Vigil

Agent reliability and evaluation platform. Traces, tests, and scores AI agents
and RAG pipelines and detects quality regressions with statistical confidence.
An investigation agent that diagnoses regressions is planned, not built.
This is a portfolio project: code quality, tests, and measured results matter
more than feature count.

## Architecture
Implemented:
- ingest/      Go service: receives OpenTelemetry traces (gRPC + HTTP), validates,
               batches, publishes to Redpanda (Kafka API); consumer writes to ClickHouse
- sdk-python/  Python tracing SDK built on OpenTelemetry
- engine/      Python eval engine: orchestrator + workers, deterministic, retrieval and
               τ² reward scorers, paired bootstrap regression gate (`engine/gate.py`, shared by the
               CLI and API), evidence bundles; read-only FastAPI service (`engine/api/`)
- dashboard/   Next.js + TypeScript + React: runs, trace viewer, run comparison with
               regression triage (affected cases, trace links, version diff)
- agents/      Agents under test: hello_agent (tool-calling), HotpotQA RAG, τ²-bench retail
- bench/       Load tests, measurement scripts, offline verdict reproduction
- deploy/      Docker Compose
- docs/        Design docs, status, results (see docs/REGRESSION_TRIAGE.md)

Planned, not implemented yet: sdk-ts/ (TypeScript SDK; the directory is a placeholder),
LLM-as-judge scorers, investigation agent, MCP server, Helm charts / Terraform.

## Infrastructure
- Redpanda (Kafka-compatible queue), ClickHouse (traces), Postgres (metadata,
  eval results), Redis (cache, job queue)
- Primary environment: the owner's Oracle Cloud ARM machine (Ubuntu 22.04, aarch64, 4 cores,
  23 GB RAM, ~40 GB free disk, no GPU). The stack runs in Docker (arm64 images); Python
  agents/engine run natively in uv venvs. Not shared; Docker containers have normal egress.
  Toolchain is user-space: Go and Node under `~/.local/{go,node}`, uv + CPython 3.12.
  The old IdeaPad (x86_64, shared) is retired from Vigil — see docs/plans/status.md
  "Environment" for its leftover workarounds. Keep memory usage reasonable.

## Conventions
- Work in small, testable steps. Every component has tests.
- Go: standard library first, table-driven tests.
- Python: type hints, pytest, ruff for linting.
- TypeScript: strict mode.
- Never commit secrets. API keys live in .env (gitignored).
- Every benchmark result is produced by a script in bench/ that saves raw data
  and generates the table, so results are reproducible.
- When unsure about a design decision, propose options with trade-offs and ask.
- Run `make test-all` (repo root) at the end of every stage before committing. It runs every
  suite across all packages and skips the live suites cleanly when the stack or
  `ANTHROPIC_API_KEY` is missing.
- Before any eval run, check free disk and refuse under 1 GB — `python -m engine run` enforces
  this (floor `VIGIL_MIN_FREE_DISK_GB`, default 1.0; the 1 GB margin was sized for the IdeaPad
  and is under review, see status.md). Never delete Docker images/volumes/build cache or
  caches without asking the owner first.
