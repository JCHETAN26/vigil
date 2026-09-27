# Vigil

Agent reliability and evaluation platform. Traces, tests, and scores AI agents
and RAG pipelines, detects quality regressions with statistical confidence, and
includes an investigation agent that diagnoses regressions and proposes fixes.
This is a portfolio project: code quality, tests, and measured results matter
more than feature count.

## Architecture
- ingest/      Go service: receives OpenTelemetry traces (gRPC + HTTP), validates,
               batches, publishes to Redpanda; consumer writes to ClickHouse
- sdk-python/  Python tracing SDK built on OpenTelemetry
- sdk-ts/      TypeScript tracing SDK
- engine/      Python (FastAPI): eval engine, LLM-as-judge, bootstrap regression
               detection, investigation agent, MCP server
- dashboard/   Next.js + TypeScript + React: trace viewer, run comparison, regressions
- agents/      Agents under test: tau-bench, HotpotQA RAG, tool-calling
- bench/       Load tests and scripts that generate results tables
- deploy/      Docker Compose, Helm charts, Terraform
- docs/        Design doc, dev log, results

## Infrastructure
- Redpanda (Kafka-compatible queue), ClickHouse (traces), Postgres (metadata,
  eval results), Redis (cache, job queue)
- Everything runs in Docker on this machine (Ubuntu 22.04, Ryzen 7 5800H,
  ~19 GiB RAM, RTX 3050 4 GB). Keep memory usage reasonable.

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
- Disk is tight and shared with the machine's owner (never touch their files). Before any eval
  run, check free disk and refuse under 1 GB — `python -m engine run` enforces this (floor
  `VIGIL_MIN_FREE_DISK_GB`, default 1.0). Never delete Docker images/volumes/build cache or
  caches without asking the owner first.
