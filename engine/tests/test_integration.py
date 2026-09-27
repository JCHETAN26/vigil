"""Stage (d): live integration test against the running stack (design §10).

Runs a 2-case local suite for hello_agent with 2 trials through the real orchestrator +
worker subprocess, then asserts:
  - one eval_runs + one eval_run_versions row, and 4 eval_case_results with the right trials;
  - each result's trace_id resolves in ClickHouse with the eval attributes
    (run_kind='eval', eval_run_id, eval_case_id, and vigil.eval.trial);
  - a measurement run with caching enabled is refused.

Postgres writes go to a **throwaway per-session database** (the ``test_db_dsn`` fixture), not
the real ``vigil`` DB — workers only emit OTLP traces (to the shared ClickHouse) and stream
results back over the pipe, so the throwaway DB is fully compatible with the live stack.

Marked ``integration``: run with ``make test-integration`` (needs the stack + ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import base64
import os
import socket
import time
import urllib.request

import psycopg
import pytest

from engine.config import EngineConfig
from engine.db.repo import CaseRow, Repo
from engine.runner.cache import CacheRefusedError
from engine.runner.orchestrator import Orchestrator

pytestmark = pytest.mark.integration


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


def _require_stack() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set (add it to the root .env)")
    if not _tcp_open("127.0.0.1", 4317):
        pytest.skip("OTLP receiver not reachable on 127.0.0.1:4317")
    try:
        if _ch("SELECT 1") != "1":
            pytest.skip("ClickHouse not answering")
    except OSError:
        pytest.skip("ClickHouse not reachable on 127.0.0.1:8123")


def _poll_count(sql: str, want: int, timeout: float = 60.0) -> int:
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


_CASES = [
    CaseRow(
        suite_id=None,
        case_id="calc-1",
        input="What is 23 * 19?",
        expected={"answer": "437", "tool_calls": ["calculator"]},
        tags=["math"],
    ),
    CaseRow(
        suite_id=None,
        case_id="wx-1",
        input="What's the weather in Paris?",
        expected={"tool_calls": ["get_weather"]},
        tags=["weather"],
    ),
]


def _seed_suite(dsn: str, name: str) -> None:
    with psycopg.connect(dsn) as conn:
        repo = Repo(conn)
        suite = repo.create_suite(name=name, adapter="local", config={"version": "itest"})
        repo.add_cases(
            [
                CaseRow(
                    suite_id=suite.id,
                    case_id=c.case_id,
                    input=c.input,
                    expected=c.expected,
                    tags=c.tags,
                )
                for c in _CASES
            ]
        )
        conn.commit()


async def test_two_case_two_trial_live(test_db_dsn):
    _require_stack()
    _seed_suite(test_db_dsn, "itest-hello")

    with psycopg.connect(test_db_dsn) as conn:
        orch = Orchestrator(conn, EngineConfig(per_case_timeout_s=60, concurrency=2))
        result = await orch.run(
            suite_name="itest-hello",
            agent_modules=["hello_agent.agent"],
            mode="development",
            trials_per_case=2,
            concurrency=2,
        )

    run_id = str(result["run_id"])

    # If the Anthropic account is usage-capped, every unit errors with a 400; report that
    # distinctly (SKIPPED), separate from a pass and from a genuine failure.
    with psycopg.connect(test_db_dsn) as conn:
        errs = conn.execute(
            "SELECT error FROM eval_case_results WHERE run_id = %s AND status != 'ok'",
            (result["run_id"],),
        ).fetchall()
    if any(e[0] and ("usage limit" in e[0].lower() or "regain access" in e[0].lower()) for e in errs):
        pytest.skip("SKIPPED: API usage cap reached (Anthropic account usage limit)")

    assert result["status"] == "succeeded", result

    # --- Postgres: one run version, 4 results with the right trial numbers ---
    with psycopg.connect(test_db_dsn) as conn:
        repo = Repo(conn)
        run_row = conn.execute(
            "SELECT status, mode, trials_per_case FROM eval_runs WHERE id = %s", (result["run_id"],)
        ).fetchone()
        assert run_row == ("succeeded", "development", 2)

        versions = repo.get_run_versions(result["run_id"])
        assert len(versions) == 1
        assert versions[0]["cases_total"] == 4
        assert versions[0]["cases_done"] == 4

        results = repo.get_case_results(result["run_id"])
        assert len(results) == 4
        trials_by_case: dict[str, set[int]] = {}
        for r in results:
            trials_by_case.setdefault(r["case_id"], set()).add(r["trial"])
        assert trials_by_case == {"calc-1": {0, 1}, "wx-1": {0, 1}}

        traces = {
            (r["case_id"], r["trial"]): r["trace_id"]
            for r in results
            if r["status"] == "ok" and r["trace_id"]
        }
    assert len(traces) == 4, f"expected 4 ok traces, got {len(traces)}"

    # --- ClickHouse: each trace resolves with the eval attributes ---
    for (case_id, trial), tid in traces.items():
        assert _poll_count(f"SELECT count() FROM spans WHERE trace_id = '{tid}'", 1) >= 1, (
            f"trace {tid} for {case_id}#{trial} did not land in ClickHouse"
        )
        # vigil.eval.trial was promoted to the typed eval_trial column (migration 005), which
        # is where the writer now records it; the raw span_attributes map no longer carries it.
        row = _ch(
            "SELECT run_kind, eval_run_id, eval_case_id, eval_trial "
            f"FROM spans WHERE trace_id = '{tid}' AND span_name = 'agent.run' LIMIT 1"
        ).split("\t")
        assert row[0] == "eval", f"run_kind on {tid}: {row[0]!r}"
        assert row[1] == run_id, f"eval_run_id on {tid}: {row[1]!r} != {run_id}"
        assert row[2] == case_id, f"eval_case_id on {tid}: {row[2]!r} != {case_id}"
        assert row[3] == str(trial), f"vigil.eval.trial on {tid}: {row[3]!r} != {trial}"

    # Aggregate join key: the whole run resolves by eval_run_id + run_kind='eval'.
    total = int(
        _ch(f"SELECT count() FROM spans WHERE eval_run_id = '{run_id}' AND run_kind = 'eval'")
    )
    assert total >= 4, f"expected >=4 eval spans for the run, got {total}"

    # --- Cost agreement: Postgres (engine cost meter) == ClickHouse (Go writer cost) ---
    # The engine prices each case's agent tokens with CostMeter.cost_with_cache and stores it in
    # eval_case_results.cost_usd; the Go writer prices each agent LLM span with CostWithCache into
    # spans.cost_usd. Both read the same prices.json (incl. cache rates), so the per-run agent
    # cost must agree. Agent spans are role='' (the user simulator, if any, is role='user_simulator').
    with psycopg.connect(test_db_dsn) as conn:
        pg_cost = float(
            conn.execute(
                "SELECT COALESCE(sum(cost_usd), 0) FROM eval_case_results WHERE run_id = %s",
                (result["run_id"],),
            ).fetchone()[0]
        )
    ch_cost = float(
        _ch(
            "SELECT sum(cost_usd) FROM spans "
            f"WHERE eval_run_id = '{run_id}' AND run_kind = 'eval' AND role = ''"
        )
        or "0"
    )
    assert pg_cost > 0, "expected non-zero agent cost"
    assert abs(pg_cost - ch_cost) < 1e-6, (
        f"agent cost disagrees: Postgres ${pg_cost} vs ClickHouse ${ch_cost}"
    )


async def test_measurement_with_cache_is_refused(test_db_dsn):
    # No API/stack needed: the refusal fires before any run row is created (design §7).
    _seed_suite(test_db_dsn, "itest-refuse")
    with psycopg.connect(test_db_dsn) as conn:
        orch = Orchestrator(conn, EngineConfig())
        # The throwaway DB is shared across this session's tests, so assert a *delta*: the
        # refused call must create no new run row (the check fires before create_run).
        before = conn.execute("SELECT count(*) FROM eval_runs").fetchone()[0]
        with pytest.raises(CacheRefusedError):
            await orch.run(
                suite_name="itest-refuse",
                agent_modules=["hello_agent.agent"],
                mode="measurement",
                cache_mode="read_write",
                trials_per_case=1,
            )
        after = conn.execute("SELECT count(*) FROM eval_runs").fetchone()[0]
        assert after == before
