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
- demo/        No-key portfolio demo: frozen REAL HotpotQA pilot evidence + offline verifier,
               SYNTHETIC fixture seeded into the real stores, launcher (`make demo`; demo/README.md)
- deploy/      Docker Compose
- docs/        Design docs, status, results (see docs/REGRESSION_TRIAGE.md)

Planned, not implemented yet: sdk-ts/ (TypeScript SDK; the directory is a placeholder),
LLM-as-judge scorers, investigation agent, MCP server, Helm charts / Terraform.

## Infrastructure
- Redpanda (Kafka-compatible queue), ClickHouse (traces), Postgres (metadata,
  eval results), Redis (cache, job queue)
- Product defaults are portable: the stack runs in Docker Compose, Python agents/engine run
  natively in uv venvs, and the API/dashboard bind loopback (8080/3000 by default).
- Current validation environment (2026-10-25): native over SSH on an x86_64 Linux host (the
  IdeaPad, shared), worktree `/home/grv22/projects/vigil-work/repo`; its own local stack (Kafka
  4.1 in place of the pinned Redpanda, ClickHouse, Postgres with read-only roles), Python 3.12,
  Node 22. A checkout on an external SSD is read-only: never write to or remount it. Disk is
  tight there; keep memory and disk usage reasonable.
- History: from 2026-09-27 the owner's Oracle Cloud ARM machine (aarch64) was the primary
  environment; its measurements in docs/plans/status.md are dated history. Whether it is still
  in use is not recorded; do not assume it was decommissioned.

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
  this (floor `VIGIL_MIN_FREE_DISK_GB`, default 1.0, sized for the IdeaPad's shared disk). Never
  delete Docker images/volumes/build cache or caches without asking the owner first.
- Paid model runs need the owner's approval; routine, reversible code changes do not.
- Demo data stays honest: seeded examples are SYNTHETIC (named `vigil-demo-synthetic-*`, notes say
  so, no model calls); only measured runs and the frozen pilot evidence are described as real.
