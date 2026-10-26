# Testing

All targets are in the root `Makefile` and source the root `.env` if present (values are never
printed). A gate that cannot run prints `SKIP <target>: NOT RUN (...)`, naming each missing
requirement, and exits 0. A skip is not a pass.

Two switches turn skips into failures (any non-empty value counts):
- `REQUIRE_LIVE=1` makes a skipped `test-infra`/`test-paid` gate fail. These are the only Makefile
  gates it controls; it does not make `test-unit` need the stack or an API key. The engine's
  Postgres fixtures also honour it (see next item), since a live run needs Postgres anyway.
- `REQUIRE_DB=1` makes the engine's Postgres-backed tests (`engine/tests/conftest.py`:
  `test_db_dsn`, `empty_db_dsn` and the `pg_conn`/`async_pg_conn` fixtures built on them) fail
  instead of skip when Postgres is unconfigured (`pg_dsn()` raises), unreachable, or refuses
  `CREATE DATABASE`. Without it (and without `REQUIRE_LIVE`) they skip, and `-rs` (on in the root and
  engine `make` targets) prints the reason. It affects only those fixtures, not the gates. Reasons
  name the error class and libpq message with the DSN and password redacted.

| Target | Runs | Needs |
|---|---|---|
| `make test-unit` | `test-python` (SDK, engine, three agents offline, bench, `test-demo`), `test-go` (ingest units), `test-dashboard` (typecheck) | venvs installed; engine DB tests (including the demo seeder's, `engine/tests/test_demo_seed.py`) self-skip without Postgres unless `REQUIRE_DB`/`REQUIRE_LIVE` is set |
| `make test-demo` | `demo/tests`: the SYNTHETIC fixture under the production gate, the frozen pilot evidence and its tamper refusal, the launcher's guards | engine venv; no DB, no network |
| `make demo-evidence` | `demo/evidence.py verify` on the committed pilot bundle (`DEMO_PY` picks the interpreter) | Python with the SDK + `engine[api]` (SciPy); no DB, no key |
| `make test-infra` | ingest Go integration; every `integration`-marked engine test except `test_integration.py` (API + DB) | Postgres, ClickHouse HTTP + native, Redpanda, `POSTGRES_PASSWORD`, `CLICKHOUSE_PASSWORD`. Runs with `ANTHROPIC_API_KEY` **unset** |
| `make test-paid` | engine `test_integration.py` (real model calls), hello/hotpotqa/tau2 agent e2e | the above Postgres/ClickHouse HTTP, OTLP receiver on 127.0.0.1:4317, `ANTHROPIC_API_KEY`. **Spends API credit** |
| `make test-live` | `test-infra` then `test-paid` | |
| `make test-all` | `test-unit` then `test-live` | |
| `make test-browser` | dashboard route-mocked Playwright suite | `dashboard/node_modules`; Google Chrome, or `PLAYWRIGHT_CHROME_PATH`. Own dev server on 127.0.0.1:3317 (`VIGIL_MOCK_PORT`); the `make dev` UI (3000) and API (8080) are untouched |

Endpoint overrides that the probes honour: `POSTGRES_HOST`/`POSTGRES_PORT` (or `VIGIL_PG_DSN`),
`CLICKHOUSE_HOST`/`CLICKHOUSE_HTTP_PORT`, `VIGIL_CLICKHOUSE_ADDR`, `VIGIL_KAFKA_BROKERS` (first
broker). The paid suites hardcode `127.0.0.1:8123` and `127.0.0.1:4317`, so `test-paid` refuses to
run if `CLICKHOUSE_HOST`, `CLICKHOUSE_HTTP_PORT` or `VIGIL_OTLP_GRPC_ADDR` point somewhere else.

Inside the suites, the agents' e2e tests still self-skip on their own conditions: hotpotqa without
the built corpus (`VIGIL_HOTPOTQA_CORPUS`), tau2 without `deps/tau2-bench/.venv`. `-rs` prints
every skip reason.

## CI (`.github/workflows/ci.yml`)

There are no API keys, no ClickHouse and no Redpanda in CI, so `test-infra` and `test-paid` do not
run there.
- **go**: `go vet -tags=integration ./...` (this also type-checks the integration tests), `go test ./...`, `go test -race ./...`.
- **python**: Python 3.12, one shared venv at `engine/.venv` (sdk[dev], engine[dev,api], three
  agents[dev]; no tau2-bench), symlinked into the other packages. A `postgres:16.4-alpine` service
  lets the engine DB tests actually run; a pre-check fails the job if Postgres is unreachable, and
  `REQUIRE_DB=1` makes any DB fixture that still cannot provision fail rather than skip. Then
  `make test-python` (`PYTEST_ADDOPTS=-rs` prints any remaining skip reasons).
- **evidence**: an empty venv with **only** `./sdk-python` and `./engine[api]` (no agents, no
  `bm25s`, no dev extras; the job fails if an agent package came along), then
  `make demo-evidence DEMO_PY=<that venv>` and `bench/regression_verdict.py reproduce` on the
  committed pilot bundle. Shows a fresh clone can re-verify the real evidence with the engine's
  declared dependencies alone.
- **dashboard**: `npm ci`, typecheck, `next build`, then Playwright's chromium (passed through
  `PLAYWRIGHT_CHROME_PATH`) for `make test-browser` (including `e2e/mocked/demo.spec.ts`: demo
  labels and the optional API proxy against a stand-in upstream on 127.0.0.1:3319).

Hosted CI results are on the open PRs' checks pages (PR #1 and the stacked PR #2). A dated local
record (including `npm run build:isolated`, a production build into `.next-build` that leaves a
running dev server's `.next` alone) is in
[`REGRESSION_TRIAGE.md` §7](./REGRESSION_TRIAGE.md#7-validation-record-2026-10-23-synthetic-check-and-final-local-checks).

Actions are pinned to explicit release tags (`checkout@v7.0.1`, `setup-go@v7.0.0`,
`setup-python@v7.1.0`, `setup-node@v7.1.0`, `upload-artifact@v7.0.2`), not floating majors; bump
them deliberately and rerun actionlint.
