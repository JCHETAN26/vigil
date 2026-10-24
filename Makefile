# Vigil — top-level test aggregator. Run at the end of every stage.
#
# `make test-all` = test-unit + test-live:
#   - test-unit: offline Python suites, Go units, dashboard typecheck. Always runs.
#   - test-live = test-infra + test-paid, each gated on its own requirements:
#       test-infra  ingest Go integration + engine API/DB integration (every `integration`-marked
#                   engine test except test_integration.py). Needs the stack, NOT an API key; it
#                   runs with ANTHROPIC_API_KEY unset so nothing in it can spend money.
#       test-paid   engine test_integration.py (real model calls through the worker) + the three
#                   agent e2e suites. Needs the stack AND ANTHROPIC_API_KEY.
#     A gate whose requirements are missing prints "SKIP ... NOT RUN" naming each missing piece
#     and exits 0, so test-all still finishes on a machine without the stack. A skip is not a pass:
#     set REQUIRE_LIVE=1 to make a skipped gate fail instead (the engine DB fixtures also honour it).
#   - The engine's Postgres-backed tests in test-unit self-skip, with reasons, when Postgres is
#     unusable; REQUIRE_DB=1 makes them fail instead. REQUIRE_DB does not affect the live gates.
#   - test-browser (not part of test-all): the route-mocked Playwright suite; needs a browser.
#
# Endpoints honour the same overrides the code reads (POSTGRES_HOST/POSTGRES_PORT or VIGIL_PG_DSN,
# CLICKHOUSE_HOST/CLICKHOUSE_HTTP_PORT, VIGIL_CLICKHOUSE_ADDR, VIGIL_KAFKA_BROKERS), so a probe
# never passes on one port while the tests talk to another. The paid suites hardcode
# 127.0.0.1:8123 / 127.0.0.1:4317, so test-paid refuses to run if those are overridden.
SHELL := /bin/bash

ROOT := $(CURDIR)
ENV := $(ROOT)/.env
LOADENV := set -a; [ -f $(ENV) ] && . $(ENV); set +a;
ISOLATE := env -u PYTHONPATH
ENGINE_PY := $(ROOT)/engine/.venv/bin/python
PYTEST := $(ISOLATE) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(ENGINE_PY) -m pytest -q -rs -p asyncio

# Shell helpers for the live gates (no output; exit status only, so no credential is ever printed).
#   tcp_up HOST:PORT         something accepts a TCP connection
#   ch_up HOST PORT          ClickHouse HTTP answers /ping with "Ok."
#   pg_up                    engine.config.pg_dsn() connects (verifies it is Postgres + the creds)
#   miss_if_stack_down       appends to $miss each missing Postgres/ClickHouse piece (incl. unset
#                            passwords, by name only)
PROBES := \
  tcp_up() { timeout 3 bash -c 'exec 3<>"/dev/tcp/$$0/$$1"' "$${1%:*}" "$${1\#\#*:}" 2>/dev/null; }; \
  ch_up() { $(ISOLATE) $(ENGINE_PY) -c 'import sys, urllib.request as u; \
    sys.exit(u.urlopen(f"http://{sys.argv[1]}:{sys.argv[2]}/ping", timeout=3).read() != b"Ok.\n")' "$$1" "$$2" >/dev/null 2>&1; }; \
  pg_up() { $(ISOLATE) $(ENGINE_PY) -c 'import psycopg; from engine.config import pg_dsn; \
    psycopg.connect(pg_dsn(), connect_timeout=3).close()' >/dev/null 2>&1; }; \
  miss_if_stack_down() { \
    pg_where="$${POSTGRES_HOST:-127.0.0.1}:$${POSTGRES_PORT:-5432}"; [ -z "$${VIGIL_PG_DSN:-}" ] || pg_where="VIGIL_PG_DSN"; \
    if [ -z "$${VIGIL_PG_DSN:-}$${POSTGRES_PASSWORD:-}" ]; then miss="$$miss POSTGRES_PASSWORD(unset)"; \
    elif ! pg_up; then miss="$$miss Postgres($$pg_where)"; fi; \
    [ -n "$${CLICKHOUSE_PASSWORD:-}" ] || miss="$$miss CLICKHOUSE_PASSWORD(unset)"; \
    ch_up "$${CLICKHOUSE_HOST:-127.0.0.1}" "$${CLICKHOUSE_HTTP_PORT:-8123}" || \
      miss="$$miss ClickHouse-HTTP($${CLICKHOUSE_HOST:-127.0.0.1}:$${CLICKHOUSE_HTTP_PORT:-8123})"; \
  }; \
  skip_gate() { \
    echo "==> SKIP $$1: NOT RUN ($$2). Unavailable:$$miss"; \
    echo "    Start the stack with 'cd ingest && make up' and set the missing values in .env."; \
    [ -z "$(REQUIRE_LIVE)" ] || { echo "REQUIRE_LIVE is set: failing instead of skipping." >&2; exit 1; }; \
  };

.PHONY: test-all test-unit test-python test-bench test-go test-dashboard test-live test-infra test-paid test-browser dev

# Every gate shares one venv and one stack; never run them concurrently under `make -j`.
.NOTPARALLEL:

test-all: test-unit test-live

# Start the read-only API (127.0.0.1:8080) and the dashboard dev server (127.0.0.1:3000)
# together; Ctrl-C stops both. Loads .env for the API's DB credentials.
dev:
	@$(LOADENV) bash -c 'set -m; \
	  ( cd $(ROOT)/engine && env -u PYTHONPATH .venv/bin/python -m engine.api ) & apipid=$$!; \
	  ( cd $(ROOT)/dashboard && npm run dev ) & webpid=$$!; \
	  echo "API pid $$apipid on 127.0.0.1:8080; dashboard pid $$webpid on 127.0.0.1:3000. Ctrl-C stops both."; \
	  trap "kill $$apipid $$webpid 2>/dev/null" INT TERM EXIT; \
	  wait'

# Always-runnable suites (no API key). The engine's Postgres-backed tests self-skip when
# Postgres is unconfigured, unreachable or refuses CREATE DATABASE; run with the stack up (or a
# Postgres service, as in CI) to execute them, with REQUIRE_DB=1 to fail rather than skip.
test-unit: test-python test-go test-dashboard

test-python:
	@echo "==> sdk-python (unit)";        $(MAKE) -s -C sdk-python test
	@echo "==> engine (unit)";            $(LOADENV) $(MAKE) -s -C engine test
	@echo "==> hello_agent (offline)";    $(MAKE) -s -C agents/hello_agent test
	@echo "==> hotpotqa_agent (offline)"; $(MAKE) -s -C agents/hotpotqa_agent test
	@echo "==> tau2_retail_agent (offline)"; $(MAKE) -s -C agents/tau2_retail_agent test
	@$(MAKE) -s test-bench

test-bench:
	@echo "==> bench (aggregation)";      $(PYTEST) bench/tests

test-go:
	@echo "==> ingest (Go units)";        cd ingest && go test ./...

test-dashboard:
	@echo "==> dashboard (typecheck)";    if [ -d dashboard/node_modules ]; then (cd dashboard && npm run -s typecheck); else echo "SKIP dashboard typecheck: NOT RUN (dashboard/node_modules absent; run 'npm ci' in dashboard/)"; fi

test-live: test-infra test-paid

# Stack-only integration: no API key, no model calls.
test-infra:
	@set -e; $(LOADENV) $(PROBES) \
	miss=""; \
	if [ ! -x $(ENGINE_PY) ]; then miss=" engine-venv($(ENGINE_PY); run 'make -C engine install')"; else miss_if_stack_down; fi; \
	tcp_up "$${VIGIL_CLICKHOUSE_ADDR:-127.0.0.1:9000}" || miss="$$miss ClickHouse-native($${VIGIL_CLICKHOUSE_ADDR:-127.0.0.1:9000})"; \
	kafka="$${VIGIL_KAFKA_BROKERS:-127.0.0.1:19092}"; kafka="$${kafka%%,*}"; \
	tcp_up "$$kafka" || miss="$$miss Redpanda($$kafka)"; \
	if [ -n "$$miss" ]; then skip_gate test-infra "ingest Go integration, engine API/DB integration"; exit 0; fi; \
	echo "==> ingest (Go integration)";   env -u ANTHROPIC_API_KEY $(MAKE) -s -C ingest test-integration; \
	echo "==> engine (API/DB integration, no API key)"; \
	cd engine && env -u ANTHROPIC_API_KEY $(PYTEST) -m integration --ignore=tests/test_integration.py

# Real model calls: engine worker integration + agent e2e. Spends API credit.
test-paid:
	@set -e; $(LOADENV) $(PROBES) \
	for v in CLICKHOUSE_HOST:127.0.0.1 CLICKHOUSE_HTTP_PORT:8123 VIGIL_OTLP_GRPC_ADDR:127.0.0.1:4317; do \
	  name="$${v%%:*}"; want="$${v#*:}"; \
	  if [ -n "$${!name:-}" ] && [ "$${!name}" != "$$want" ]; then \
	    echo "test-paid: $$name=$${!name}, but the paid suites hardcode $$want; refusing to run them against a different service." >&2; exit 1; \
	  fi; \
	done; \
	miss=""; \
	[ -n "$${ANTHROPIC_API_KEY:-}" ] || miss="$$miss ANTHROPIC_API_KEY(unset)"; \
	if [ ! -x $(ENGINE_PY) ]; then miss="$$miss engine-venv($(ENGINE_PY); run 'make -C engine install')"; else miss_if_stack_down; fi; \
	tcp_up 127.0.0.1:4317 || miss="$$miss OTLP-receiver(127.0.0.1:4317)"; \
	if [ -n "$$miss" ]; then skip_gate test-paid "engine model integration, hello/hotpotqa/tau2 agent e2e"; exit 0; fi; \
	echo "==> engine (model integration)"; (cd engine && $(PYTEST) -m integration tests/test_integration.py); \
	echo "==> hello_agent (e2e)";         $(MAKE) -s -C agents/hello_agent test-e2e; \
	echo "==> hotpotqa_agent (e2e)";      $(MAKE) -s -C agents/hotpotqa_agent test-e2e; \
	echo "==> tau2_retail_agent (e2e)";   $(MAKE) -s -C agents/tau2_retail_agent test-e2e

# Route-mocked dashboard browser suite (dashboard/playwright.mocked.config.ts). Starts its own
# dev server on 127.0.0.1:VIGIL_MOCK_PORT (default 3317), so the `make dev` UI (3000) and API (8080)
# are unaffected; uses installed Google Chrome, or the binary at PLAYWRIGHT_CHROME_PATH (CI points
# that at Playwright's chromium). Downloads no browser.
test-browser:
	@set -e; \
	if [ ! -d dashboard/node_modules ]; then echo "test-browser: dashboard/node_modules absent; run 'npm ci' in dashboard/" >&2; exit 1; fi; \
	echo "==> dashboard (mocked browser)"; cd dashboard && npm run -s e2e:mocked
