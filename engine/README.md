# Vigil eval engine

Runs a versioned agent under test over a dataset suite, scores each case with deterministic
checks, records per-case results in Postgres, and links every result back to its ClickHouse
trace. See [`docs/design/eval-engine.md`](../docs/design/eval-engine.md) for the design.

Runs **natively on the host** (not containerized): it calls the Anthropic API over the host
network and reaches the stack via the published `127.0.0.1` ports (OTLP `4317`/`4318`,
Postgres `5432`, ClickHouse `8123`/`9000`, Redis `6379`).

## Setup

```sh
make install     # venv + engine + vigil-sdk (editable) + agent packages
make test        # unit + live-Postgres tests (skips DB tests if Postgres is down)
make migrate     # apply Postgres migrations to VIGIL_PG_DSN / POSTGRES_*
```

Source the repo `.env` first so `POSTGRES_*` / `REDIS_PASSWORD` / `ANTHROPIC_API_KEY` are set.

## Agent dependencies (important)

The engine evaluates each agent version in **its own worker subprocess**, and those
subprocesses run **inside the engine's venv** (`engine/.venv`). So every agent under test
must have its runtime dependencies installed there — `make install` installs `hello_agent`
into the engine venv for this reason (add other agent packages the same way, e.g.
`.venv/bin/pip install --no-deps -e ../agents/<name>`).

Because agent code and engine code share one interpreter, **`anthropic` is pinned to the same
version range (`>=1.8,<2`) in `engine/pyproject.toml`, `agents/hello_agent`, and every other
agent package.** The version an agent is tested against is therefore the version the worker
runs it with. Agents **must not import the engine** — the contract flows one way (the engine
imports the agent). Agents report token counts only; the engine computes cost.

## Migrations

Raw SQL in `engine/db/migrations/*.sql` is the source of truth (mirrors the Go ingest
migrator). `engine/db/migrate.py` applies numbered files in order, recording each file's name
and SHA-256 checksum in a `schema_migrations` table. It is idempotent, and it **fails loudly**
if a file that was already applied has since changed — migrations are immutable once applied;
put fixes in a new numbered file.
