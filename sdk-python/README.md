# Vigil Python SDK

A thin layer over the OpenTelemetry Python SDK that exports OTLP traces of agent runs,
LLM calls, tool calls, and retrieval steps to the Vigil ingest service, using the
`gen_ai.*` GenAI conventions and the `vigil.*` identity attributes from the design doc.

## Quick start

```python
import vigil

vigil.init(
    service_name="support-agent",
    agent_id="tau-bench-retail",
    agent_version=vigil.compute_agent_version(
        prompts={"system": SYSTEM_PROMPT},
        tools=TOOLS,
        model="claude-haiku-4-5",
        params={"temperature": 0.0, "max_tokens": 1024},
        code_paths=["agent.py"],
    ),
    git_sha=vigil.git_sha(),
)

from anthropic import Anthropic
client = vigil.wrap(Anthropic())  # also wraps AsyncAnthropic; streaming supported

with vigil.agent_run(run_kind="eval", eval_run_id="batch-1", eval_case_id="q-7"):
    with vigil.retrieval(query="..."):
        ...
    client.messages.create(model="claude-haiku-4-5", max_tokens=1024, messages=[...])
    with vigil.tool_call(tool_name="lookup_order", arguments={...}):
        ...
```

`init()` registers an atexit flush + shutdown, so a short-lived script's spans are never
dropped. Run identity is stamped onto **every** span via a span processor, and
`run_id` defaults to the trace id.

## Configuration (env or `Config`)

| Env var | Default | Meaning |
|---|---|---|
| `VIGIL_OTLP_PROTOCOL` | `grpc` | `grpc` (:4317) or `http` (:4318) |
| `VIGIL_OTLP_ENDPOINT` | grpc `http://localhost:4317` | exporter endpoint (falls back to `OTEL_EXPORTER_OTLP_ENDPOINT`) |
| `VIGIL_CAPTURE_CONTENT_EVAL` | `true` | capture prompt/response content for eval runs |
| `VIGIL_CAPTURE_CONTENT_LIVE` | `false` | opt-in content capture for live runs |
| `VIGIL_MAX_CONTENT_BYTES` | `8192` | truncate captured content beyond this; mark it truncated |
| `VIGIL_SERVICE_NAME` / `VIGIL_AGENT_ID` / `VIGIL_AGENT_VERSION` / `VIGIL_AGENT_GIT_SHA` | — | identity |

Pass `redactor=<callable>` to `init()` / `Config` to scrub content before truncation and
export.

## Install exporters

The OTLP exporter is an optional extra (kept out of the core deps so tests run without a
transport):

```bash
pip install "vigil-sdk[otlp-grpc]"   # or [otlp-http]
```

## Development

```bash
make install   # venv + editable install with dev deps
make test      # pytest (isolated from the host ROS PYTHONPATH)
make lint      # ruff
```
