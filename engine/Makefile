# Vigil eval engine.
# Note: like the SDK, this host has ROS on PYTHONPATH, which drags system packages and a
# pytest plugin into the venv. The test/lint targets drop PYTHONPATH and disable pytest
# plugin autoload so the venv stays isolated.

VENV = .venv
PY = $(VENV)/bin/python
ISOLATE = env -u PYTHONPATH

.PHONY: venv install test test-integration lint fmt migrate clean

venv:
	python3 -m venv $(VENV)

# Install the engine, the SDK (editable path dep), and the agents whose code the worker
# runs in this venv. hello_agent is installed with --no-deps because its anthropic pin is
# already satisfied here; add other agent packages the same way.
install: venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e "../sdk-python"
	$(PY) -m pip install -q -e ".[dev]"
	$(PY) -m pip install -q --no-deps -e "../agents/hello_agent"

test:
	$(ISOLATE) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PY) -m pytest -q -p asyncio -m "not integration"

test-integration:
	$(ISOLATE) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PY) -m pytest -q -p asyncio -m integration

lint:
	$(ISOLATE) $(VENV)/bin/ruff check engine tests

fmt:
	$(ISOLATE) $(VENV)/bin/ruff format engine tests

migrate:
	$(ISOLATE) $(PY) -m engine.db.migrate

clean:
	rm -rf $(VENV) build dist *.egg-info
