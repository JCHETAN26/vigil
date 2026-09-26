# HotpotQA RAG agent under test.
# Like the SDK, this host has ROS on PYTHONPATH; the test/lint targets drop it and disable
# pytest plugin autoload so the venv stays isolated.

VENV = .venv
PY = $(VENV)/bin/python
# uv-managed CPython (installed with `uv python install 3.12`); the host system Python is untouched.
PYTHON_VERSION = 3.12
ISOLATE = env -u PYTHONPATH

.PHONY: venv install test lint fmt clean

venv:
	uv venv --python $(PYTHON_VERSION) --seed $(VENV)

install: venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e "../../sdk-python"
	$(PY) -m pip install -q -e ".[dev]"

test:
	$(ISOLATE) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PY) -m pytest -q -m "not e2e"

lint:
	$(ISOLATE) $(VENV)/bin/ruff check hotpotqa_agent tests

fmt:
	$(ISOLATE) $(VENV)/bin/ruff format hotpotqa_agent tests

clean:
	rm -rf $(VENV) build dist *.egg-info
