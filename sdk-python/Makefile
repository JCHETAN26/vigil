# Vigil Python SDK.
# Note: this host has ROS on PYTHONPATH, which drags system packages and a pytest plugin
# into the venv. The test/lint targets drop PYTHONPATH and disable pytest plugin autoload
# so the venv stays isolated.

VENV = .venv
PY = $(VENV)/bin/python
ISOLATE = env -u PYTHONPATH

.PHONY: venv install test lint fmt clean

venv:
	python3 -m venv $(VENV)

install: venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e ".[dev]"

test:
	$(ISOLATE) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PY) -m pytest -q

lint:
	$(ISOLATE) $(VENV)/bin/ruff check src tests

fmt:
	$(ISOLATE) $(VENV)/bin/ruff format src tests

clean:
	rm -rf $(VENV) build dist *.egg-info src/*.egg-info
