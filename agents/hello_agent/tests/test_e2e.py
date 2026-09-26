"""End-to-end test: run the hello agent against the running Vigil stack and verify the
resulting trace in ClickHouse — one complete trace, the agent-run span with correctly
nested LLM and tool child spans, non-zero token counts and cost, captured content, and the
agent_version present.

Skips unless ANTHROPIC_API_KEY is set and the stack (OTLP receiver + ClickHouse) is
reachable. Uses two short questions to keep API usage minimal."""

from __future__ import annotations

import base64
import os
import socket
import time
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.e2e


def _ch(sql: str) -> str:
    user = os.getenv("CLICKHOUSE_USER", "vigil")
    pw = os.getenv("CLICKHOUSE_PASSWORD", "")
    db = os.getenv("CLICKHOUSE_DB", "vigil")
    req = urllib.request.Request(
        f"http://127.0.0.1:8123/?database={db}",
        data=(sql + "\nFORMAT TabSeparated").encode(),
        method="POST",
    )
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode().strip()


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _require_stack():
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set (add it to the root .env)")
    if not _tcp_open("127.0.0.1", 4317):
        pytest.skip("OTLP receiver not reachable on 127.0.0.1:4317 (run `make up` in ingest/)")
    try:
        if _ch("SELECT 1") != "1":
            pytest.skip("ClickHouse not answering")
    except OSError:
        pytest.skip("ClickHouse not reachable on 127.0.0.1:8123")


def _poll(sql: str, want: int, timeout: float = 45.0) -> int:
    deadline = time.time() + timeout
    last = 0
    while time.time() < deadline:
        try:
            last = int(_ch(sql) or "0")
        except (OSError, ValueError):
            last = 0
        if last >= want:
            return last
        time.sleep(1.0)
    return last


def test_hello_agent_end_to_end():
    _require_stack()

    import vigil

    from hello_agent import agent

    agent.init_tracing()
    client = agent.make_client()
    run_id = "e2e-" + uuid.uuid4().hex[:12]

    # Two short questions: one forces the calculator, one forces the weather tool.
    calc = agent.run(client, "What is 23 * 19?", eval_run_id=run_id, eval_case_id="calc")
    weather = agent.run(
        client, "What's the weather in Paris?", eval_run_id=run_id, eval_case_id="weather"
    )

    vigil.shutdown()  # flush the SDK's buffered spans to the receiver

    tid = calc["trace_id"]

    # Wait until the run + its children have landed (writer batches on a short interval).
    assert _poll(f"SELECT count() FROM spans WHERE trace_id = '{tid}'", 3) >= 3

    # 1) One complete trace.
    assert int(_ch(f"SELECT uniqExact(trace_id) FROM spans WHERE trace_id = '{tid}'")) == 1

    # 2) The agent-run span is the trace root (no parent), with LLM and tool child spans.
    run_span_id = _ch(
        f"SELECT span_id FROM spans WHERE trace_id = '{tid}' AND span_name = 'agent.run' LIMIT 1"
    )
    assert run_span_id, "no agent.run span found"
    root_parent = _ch(
        f"SELECT parent_span_id FROM spans WHERE trace_id = '{tid}' AND span_id = '{run_span_id}'"
    )
    assert root_parent == "", f"agent.run should be the trace root, got parent {root_parent!r}"

    llm_children = int(
        _ch(
            f"SELECT count() FROM spans WHERE trace_id = '{tid}' AND parent_span_id = '{run_span_id}' "
            f"AND span_name LIKE 'gen_ai.chat%'"
        )
    )
    assert llm_children >= 1, "expected LLM child span(s) nested under the agent run"

    tool_children = int(
        _ch(
            f"SELECT count() FROM spans WHERE trace_id = '{tid}' AND parent_span_id = '{run_span_id}' "
            f"AND startsWith(span_name, 'tool.')"
        )
    )
    assert tool_children >= 1, "expected tool child span(s) nested under the agent run"

    # 3) Token counts and non-zero cost on the LLM spans.
    row = _ch(
        f"SELECT sum(gen_ai_usage_input_tokens), sum(gen_ai_usage_output_tokens), sum(cost_usd) "
        f"FROM spans WHERE trace_id = '{tid}' AND span_name LIKE 'gen_ai.chat%'"
    ).split("\t")
    in_tok, out_tok, cost = int(row[0]), int(row[1]), float(row[2])
    assert in_tok > 0 and out_tok > 0, f"token counts not recorded: in={in_tok} out={out_tok}"
    assert cost > 0, f"cost should be non-zero for a priced model, got {cost}"

    # 4) Content captured (run_kind=eval) — the LLM span carries gen_ai.* events.
    events = int(
        _ch(
            f"SELECT sum(length(`events.name`)) FROM spans WHERE trace_id = '{tid}' AND span_name LIKE 'gen_ai.chat%'"
        )
    )
    assert events > 0, "expected captured content events on the LLM span(s)"

    # 5) agent_version present and matching the computed version.
    av = _ch(f"SELECT DISTINCT agent_version FROM spans WHERE trace_id = '{tid}'")
    assert av == calc["agent_version"] and av != ""

    # The second question produced its own trace with a tool span (weather).
    tid2 = weather["trace_id"]
    assert (
        _poll(
            f"SELECT count() FROM spans WHERE trace_id = '{tid2}' AND startsWith(span_name, 'tool.')",
            1,
        )
        >= 1
    )
