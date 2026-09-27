# τ²-bench retail agent under test. Runs natively on the host; the test/e2e targets source the
# root .env and drop the host ROS PYTHONPATH so the venv stays isolated.
#
# τ² is heavy (openai/litellm/pandas) and pinned to a commit; it lives in its OWN venv under
# deps/tau2-bench and is reached over a subprocess domain server. `make tau2-setup` clones +
# installs it; `make prefix` regenerates the byte-stable policy+tools artifact the agent reads.
VENV = .venv
PY = $(VENV)/bin/python
# uv-managed CPython (installed with `uv python install 3.12`); the host system Python is untouched.
PYTHON_VERSION = 3.12
ISOLATE = env -u PYTHONPATH
ROOT_ENV = ../../.env
LOADENV = set -a; [ -f $(ROOT_ENV) ] && . $(ROOT_ENV); set +a;

# Pinned τ² (see data/tau2/retail.pins.json). Its isolated venv + the prefix artifact.
TAU2_COMMIT = b7ea9074c1cba482b30687fecdb5c8425fd6f619
TAU2_DIR = ../../deps/tau2-bench
TAU2_PY = $(TAU2_DIR)/.venv/bin/python
PREFIX = ../../data/tau2/retail.prefix.json
SERVER = tau2_retail_agent/domain_server.py

.PHONY: venv install tau2-setup prefix test test-e2e lint clean

venv:
	uv venv --python $(PYTHON_VERSION) --seed $(VENV)

install: venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e "../../sdk-python[otlp-grpc]"
	$(PY) -m pip install -q -e ".[dev]"

# Clone τ² at the pinned commit and install it (+websockets, needed at import) into its OWN venv.
tau2-setup:
	@if [ ! -d $(TAU2_DIR)/.git ]; then \
	  git clone https://github.com/sierra-research/tau2-bench.git $(TAU2_DIR); \
	fi
	git -C $(TAU2_DIR) fetch --quiet origin $(TAU2_COMMIT) || true
	git -C $(TAU2_DIR) checkout --quiet $(TAU2_COMMIT)
	uv venv --python $(PYTHON_VERSION) --seed $(TAU2_DIR)/.venv
	$(ISOLATE) $(TAU2_DIR)/.venv/bin/pip install -q -e $(TAU2_DIR) websockets

# Regenerate the pinned prefix artifact (policy + tool schemas) from the τ² domain server.
prefix:
	@printf '%s\n' '{"op":"prefix"}' '{"op":"shutdown"}' | $(ISOLATE) $(TAU2_PY) $(SERVER) 2>/dev/null | head -1 | \
	  $(PY) -c "import sys,json; d=json.load(sys.stdin); assert d['ok']; \
	    open('$(PREFIX)','w').write(json.dumps({'pinned_commit':'$(TAU2_COMMIT)','domain':'retail','policy':d['policy'],'tools':d['tools']}, indent=2, ensure_ascii=False)+chr(10)); \
	    print('wrote $(PREFIX): tools=%d policy_chars=%d' % (len(d['tools']), len(d['policy'])))"

test:
	$(ISOLATE) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PY) -m pytest -q -m "not e2e"

# Live end-to-end against the running stack + τ² venv (needs ANTHROPIC_API_KEY); skips cleanly if absent.
test-e2e:
	$(LOADENV) $(ISOLATE) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PY) -m pytest -q -rs -m e2e

lint:
	$(ISOLATE) $(VENV)/bin/ruff check tau2_retail_agent tests

clean:
	rm -rf $(VENV) build dist *.egg-info
