"""End-to-end test: run the HotpotQA agent against the running Vigil stack on ONE question
and verify the resulting trace in ClickHouse — one complete trace, the agent-run span as
root with nested LLM and retrieval (search) child spans, non-zero tokens and cost, and the
agent_version present.

Skips cleanly unless ANTHROPIC_API_KEY is set, the stack (OTLP receiver + ClickHouse) is
reachable, and the built corpus is available via VIGIL_HOTPOTQA_CORPUS. One question keeps
API usage minimal."""

from __future__ import annotations

import base64
import os
import socket
import time
import urllib.request
import uuid
from pathlib import Path

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


def _skip_if_api_capped(exc: BaseException) -> None:
    """If an exception is Anthropic's account usage-cap 400, skip loudly (distinct from a pass
    and from a real failure); otherwise return so the caller re-raises the genuine error."""
    text = str(exc).lower()
    if "usage limit" in text or "regain access" in text:
        pytest.skip("SKIPPED: API usage cap reached (Anthropic account usage limit)")


def _require_stack_and_corpus() -> str:
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set (add it to the root .env)")
    corpus = os.getenv("VIGIL_HOTPOTQA_CORPUS")
    if not corpus or not Path(corpus).is_file():
        pytest.skip(
            "VIGIL_HOTPOTQA_CORPUS not set or missing "
            "(build with bench/build_hotpotqa_corpus.py and point it at corpus.v1.jsonl)"
        )
    if not _tcp_open("127.0.0.1", 4317):
        pytest.skip("OTLP receiver not reachable on 127.0.0.1:4317 (run `make up` in ingest/)")
    try:
        if _ch("SELECT 1") != "1":
            pytest.skip("ClickHouse not answering")
    except OSError:
        pytest.skip("ClickHouse not reachable on 127.0.0.1:8123")
    return corpus


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


def test_hotpotqa_agent_end_to_end():
    _require_stack_and_corpus()

    import asyncio

    import vigil

    from hotpotqa_agent import agent

    agent.init_tracing()
    client = agent.make_client()
    run_id = "e2e-" + uuid.uuid4().hex[:12]

    # A two-hop bridge question that needs at least one search.
    question = "Which magazine was started first, Arthur's Magazine or First for Women?"
    try:
        result = asyncio.run(agent.run(client, question, eval_run_id=run_id, eval_case_id="q0"))
    except Exception as exc:
        _skip_if_api_capped(exc)
        raise
    vigil.shutdown()  # flush buffered spans to the receiver

    assert result.final_answer, "agent produced no final answer"
    assert result.retrievals, "agent recorded no retrievals"
    tid = result.trace_id

    # The run + its children land (writer batches on a short interval).
    assert _poll(f"SELECT count() FROM spans WHERE trace_id = '{tid}'", 3) >= 3

    # 1) One complete trace.
    assert int(_ch(f"SELECT uniqExact(trace_id) FROM spans WHERE trace_id = '{tid}'")) == 1

    # 2) agent.run is the trace root, with nested LLM and search (retrieval) child spans.
    # The root span closes (and flushes) last, so poll for it rather than assume the count above
    # implies it has landed.
    assert (
        _poll(f"SELECT count() FROM spans WHERE trace_id = '{tid}' AND span_name = 'agent.run'", 1)
        >= 1
    ), "agent.run span did not land"
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

    search_children = int(
        _ch(
            f"SELECT count() FROM spans WHERE trace_id = '{tid}' AND parent_span_id = '{run_span_id}' "
            f"AND span_name = 'search'"
        )
    )
    assert search_children >= 1, "expected retrieval (search) child span(s) under the agent run"

    # 3) Token counts and non-zero cost on the LLM spans.
    row = _ch(
        f"SELECT sum(gen_ai_usage_input_tokens), sum(gen_ai_usage_output_tokens), sum(cost_usd) "
        f"FROM spans WHERE trace_id = '{tid}' AND span_name LIKE 'gen_ai.chat%'"
    ).split("\t")
    in_tok, out_tok, cost = int(row[0]), int(row[1]), float(row[2])
    assert in_tok > 0 and out_tok > 0, f"token counts not recorded: in={in_tok} out={out_tok}"
    assert cost > 0, f"cost should be non-zero for a priced model, got {cost}"

    # 4) agent_version present and matching the computed version.
    av = _ch(f"SELECT DISTINCT agent_version FROM spans WHERE trace_id = '{tid}'")
    assert av == agent.AGENT_VERSION and av != ""
