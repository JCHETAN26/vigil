"""End-to-end test: run the τ² retail agent on ONE task against the live stack + the isolated
τ² venv, and verify in ClickHouse — the trace lands, the agent-run span is the root with LLM
children, the user-simulator's LLM spans are tagged vigil.role=user_simulator, and — the point
of this test — **prompt caching engaged** (cache-creation tokens > 0 on the agent's LLM spans,
i.e. the policy+tools prefix cleared Haiku's 4096-token minimum and was written to cache).

Skips cleanly unless ANTHROPIC_API_KEY is set, the stack is reachable, and the τ² venv exists."""

from __future__ import annotations

import asyncio
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


def _require():
    from tau2_retail_agent import agent

    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    if not Path(agent._TAU2_PYTHON).exists():  # noqa: SLF001
        pytest.skip("τ² venv not set up (make tau2-setup in agents/tau2_retail_agent)")
    if not _tcp_open("127.0.0.1", 4317):
        pytest.skip("OTLP receiver not reachable on 127.0.0.1:4317")
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


def test_tau2_retail_end_to_end_with_caching():
    _require()
    import vigil

    from tau2_retail_agent import agent

    agent.init_tracing()
    client = agent.make_client()
    run_id = "e2e-" + uuid.uuid4().hex[:12]

    async def _go():
        r = await agent.run(client, {"task_id": "0"}, eval_run_id=run_id, eval_case_id="0")
        await agent._SERVER.shutdown()  # noqa: SLF001
        return r

    try:
        result = asyncio.run(_go())
    except Exception as exc:
        _skip_if_api_capped(exc)
        raise
    assert "reward" in result.info
    vigil.shutdown()
    tid = result.trace_id

    # The run + children land; agent.run is the root.
    run_spans = _poll(
        f"SELECT count() FROM spans WHERE trace_id = '{tid}' AND span_name = 'agent.run'", 1
    )
    assert run_spans >= 1
    llm = int(
        _ch(f"SELECT count() FROM spans WHERE trace_id = '{tid}' AND span_name LIKE 'gen_ai.chat%'")
    )
    assert llm >= 2, "expected agent + simulator LLM spans"

    # The simulator's calls are tagged; the agent's are not.
    sim = int(
        _ch(
            f"SELECT count() FROM spans WHERE trace_id = '{tid}' "
            f"AND span_name LIKE 'gen_ai.chat%' AND role = 'user_simulator'"
        )
    )
    assert sim >= 1, "expected user_simulator LLM span(s)"

    # THE CACHING CHECK: the agent's LLM spans (role='') wrote the policy+tools prefix to cache.
    cache_write = int(
        _ch(
            f"SELECT sum(gen_ai_usage_cache_creation_input_tokens) FROM spans "
            f"WHERE trace_id = '{tid}' AND span_name LIKE 'gen_ai.chat%' AND role = ''"
        )
        or "0"
    )
    assert cache_write > 0, (
        "prompt caching did NOT engage — no cache-creation tokens on the agent spans "
        "(prefix may be below Haiku's 4096-token minimum)"
    )
