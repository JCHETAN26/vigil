# Vigil — top-level test aggregator. Run at the end of every stage.
#
# `make test-all` runs every suite across all packages:
#   - unit / offline suites + Go units: always.
#   - live suites (ingest integration, engine integration, hello_agent e2e, hotpotqa_agent
#     e2e): only when the stack (OTLP receiver + ClickHouse) and ANTHROPIC_API_KEY are
#     present; otherwise the whole live block is skipped cleanly with a note.
#
# The Python live suites self-skip via pytest.skip when the stack/key is missing, but the Go
# integration suite hard-fails without the stack, so the block is gated on a reachability probe.
SHELL := /bin/bash

ROOT := $(CURDIR)
ENV := $(ROOT)/.env
LOADENV := set -a; [ -f $(ENV) ] && . $(ENV); set +a;
ISOLATE := env -u PYTHONPATH
BENCH_PY := $(ROOT)/engine/.venv/bin/python

.PHONY: test-all test-unit test-live dev

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

# Always-runnable suites (no external services / no API key).
test-unit:
	@echo "==> sdk-python (unit)";        $(MAKE) -s -C sdk-python test
	@echo "==> engine (unit)";            $(LOADENV) $(MAKE) -s -C engine test
	@echo "==> hello_agent (offline)";    $(MAKE) -s -C agents/hello_agent test
	@echo "==> hotpotqa_agent (offline)"; $(MAKE) -s -C agents/hotpotqa_agent test
	@echo "==> tau2_retail_agent (offline)"; $(MAKE) -s -C agents/tau2_retail_agent test
	@echo "==> bench (aggregation)";      $(ISOLATE) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(BENCH_PY) -m pytest -q -p asyncio bench/tests
	@echo "==> ingest (Go units)";        cd ingest && go test ./...
	@echo "==> dashboard (typecheck)";    if [ -d dashboard/node_modules ]; then (cd dashboard && npm run -s typecheck); else echo "SKIP: dashboard/node_modules absent (run 'npm install' in dashboard/)"; fi

# Live suites — gated on the stack + API key; skipped cleanly (exit 0) when unavailable.
test-live:
	@set -e; $(LOADENV) \
	miss=""; \
	[ -n "$$ANTHROPIC_API_KEY" ] || miss="$$miss ANTHROPIC_API_KEY"; \
	(exec 3<>/dev/tcp/127.0.0.1/4317) 2>/dev/null || miss="$$miss OTLP-receiver(:4317)"; \
	(exec 3<>/dev/tcp/127.0.0.1/8123) 2>/dev/null || miss="$$miss ClickHouse(:8123)"; \
	if [ -n "$$miss" ]; then \
	  echo "==> SKIP live suites (missing:$$miss). Start the stack (cd ingest && make up) and set ANTHROPIC_API_KEY in .env."; \
	  exit 0; \
	fi; \
	echo "==> ingest (Go integration)";   $(MAKE) -s -C ingest test-integration; \
	echo "==> engine (integration)";      $(MAKE) -s -C engine test-integration; \
	echo "==> hello_agent (e2e)";         $(MAKE) -s -C agents/hello_agent test-e2e; \
	echo "==> hotpotqa_agent (e2e)";      $(MAKE) -s -C agents/hotpotqa_agent test-e2e; \
	echo "==> tau2_retail_agent (e2e)";   $(MAKE) -s -C agents/tau2_retail_agent test-e2e
