# Week 1 hello agent. Runs natively on the host (see docs infra note). The test/run
# targets source the root .env (for ANTHROPIC_API_KEY + ClickHouse creds) and drop the
# host ROS PYTHONPATH so the venv stays isolated.

VENV = .venv
PY = $(VENV)/bin/python
ROOT_ENV = ../../.env
ISOLATE = env -u PYTHONPATH
LOADENV = set -a; [ -f $(ROOT_ENV) ] && . $(ROOT_ENV); set +a;

.PHONY: venv install run test lint clean

venv:
	python3 -m venv $(VENV)

install: venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e "../../sdk-python[otlp-grpc]"
	$(PY) -m pip install -q -e ".[dev]"

# Run the agent against the running stack (needs ANTHROPIC_API_KEY + `make up` in ingest/).
run:
	$(LOADENV) $(ISOLATE) $(PY) -m hello_agent

test:
	$(LOADENV) $(ISOLATE) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PY) -m pytest -q

lint:
	$(ISOLATE) $(VENV)/bin/ruff check hello_agent tests

clean:
	rm -rf $(VENV) build dist *.egg-info
