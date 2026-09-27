"""Read-only dashboard API (Week 4).

Security/robustness invariants:
- Every Postgres query uses bound parameters (%s); every ClickHouse query uses server-side
  parameter binding (`{name:Type}` + param_<name>). No request value is ever formatted into SQL.
- Path ids are format-validated by FastAPI before any query runs: run ids are UUIDs, trace ids
  are 32 lowercase-hex. A malformed id is a 422, never a query.
- List/table/trace endpoints are paginated and hard-bounded.
- Captured content is returned as plain strings; the client renders it as text (never HTML).

Bind to 127.0.0.1 only (see __main__).
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from engine.config import pg_ro_dsn
from engine.regression import HIGHER_IS_BETTER, METRIC_DIRECTION, paired_bootstrap_diff

from .clickhouse import ClickHouseClient, ClickHouseError

TraceId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
_MAX_TRACE_SPANS = 5000
_GATE = {"passed", "TokenF1"}
_THRESHOLDS = {"passed": 0.05, "TokenF1": 0.05}


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = AsyncConnectionPool(
        pg_ro_dsn(), min_size=1, max_size=8, open=False, kwargs={"row_factory": dict_row}
    )
    await pool.open()
    app.state.pool = pool
    app.state.ch = ClickHouseClient()
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="Vigil dashboard API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


async def _fetchall(sql: str, params: tuple = ()):
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def _fetchone(sql: str, params: tuple = ()):
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/runs")
async def list_runs(
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
):
    rows = await _fetchall(
        "SELECT r.id, s.name AS suite, r.agent_id, r.mode, r.trials_per_case, r.status, "
        "r.cost_spent_usd, r.created_at FROM eval_runs r JOIN eval_suites s ON s.id = r.suite_id "
        "ORDER BY r.created_at DESC LIMIT %s OFFSET %s",
        (limit, offset),
    )
    return {"runs": [_run_row(r) for r in rows], "limit": limit, "offset": offset}


def _run_row(r: dict) -> dict:
    return {
        "id": str(r["id"]),
        "suite": r["suite"],
        "agent_id": r["agent_id"],
        "mode": r["mode"],
        "trials_per_case": r["trials_per_case"],
        "status": r["status"],
        "cost_usd": float(r["cost_spent_usd"]) if r["cost_spent_usd"] is not None else 0.0,
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
    }


@app.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID):
    meta = await _fetchone(
        "SELECT r.id, s.name AS suite, r.agent_id, r.mode, r.trials_per_case, r.status, "
        "r.cost_spent_usd, r.created_at FROM eval_runs r JOIN eval_suites s ON s.id = r.suite_id "
        "WHERE r.id = %s",
        (run_id,),
    )
    if meta is None:
        raise HTTPException(404, "run not found")
    agg = await _fetchone(
        "SELECT count(*) AS n, count(*) FILTER (WHERE status='ok') AS n_ok, "
        "avg((passed)::int) FILTER (WHERE status='ok') AS pass_rate, "
        "avg(cost_usd) FILTER (WHERE status='ok') AS mean_cost, "
        "avg(latency_ms) FILTER (WHERE status='ok') AS mean_latency "
        "FROM eval_case_results WHERE run_id = %s",
        (run_id,),
    )
    return {
        **_run_row(meta),
        "n_cases": agg["n"],
        "n_ok": agg["n_ok"],
        "pass_rate": float(agg["pass_rate"]) if agg["pass_rate"] is not None else None,
        "mean_cost_usd": float(agg["mean_cost"]) if agg["mean_cost"] is not None else None,
        "mean_latency_ms": float(agg["mean_latency"]) if agg["mean_latency"] is not None else None,
    }


@app.get("/runs/{run_id}/cases")
async def get_cases(
    run_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    if await _fetchone("SELECT 1 FROM eval_runs WHERE id = %s", (run_id,)) is None:
        raise HTTPException(404, "run not found")
    rows = await _fetchall(
        "SELECT r.case_id, r.trial, r.status, r.passed, r.score, r.scores, r.cost_usd, "
        "r.input_tokens, r.output_tokens, r.latency_ms, r.trace_id, ec.tags "
        "FROM eval_case_results r JOIN eval_runs run ON run.id = r.run_id "
        "JOIN eval_cases ec ON ec.suite_id = run.suite_id AND ec.case_id = r.case_id "
        "WHERE r.run_id = %s ORDER BY r.case_id, r.trial LIMIT %s OFFSET %s",
        (run_id, limit, offset),
    )
    return {
        "run_id": str(run_id),
        "limit": limit,
        "offset": offset,
        "cases": [
            {
                "case_id": r["case_id"],
                "trial": r["trial"],
                "status": r["status"],
                "passed": r["passed"],
                "score": float(r["score"]) if r["score"] is not None else None,
                "scores": r["scores"],
                "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "latency_ms": r["latency_ms"],
                "trace_id": r["trace_id"],
                "tags": r["tags"],
            }
            for r in rows
        ],
    }


def _json_list(raw) -> list:
    """Parse a JSON-array string (e.g. vigil.retrieval.doc_ids) to a list; [] on empty/invalid."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def _parse_event(name: str, attrs_json: str) -> dict:
    """A captured content event → {name, content (text only), truncated}. Content is the
    recorded text (not the raw attributes JSON); truncated reflects vigil.content.truncated."""
    try:
        obj = json.loads(attrs_json) if attrs_json else {}
    except (ValueError, TypeError):
        obj = {}
    return {
        "name": name,
        "content": obj.get("content"),
        "truncated": bool(obj.get("vigil.content.truncated", False)),
        "tool_name": obj.get("gen_ai.tool.name"),
    }


@app.get("/traces/{trace_id}")
async def get_trace(trace_id: TraceId):
    try:
        rows = app.state.ch.query(
            "SELECT span_id, parent_span_id, span_name, span_kind, role, "
            "gen_ai_request_model AS model, "
            "toUnixTimestamp64Nano(start_time) AS start_ns, "
            "toUnixTimestamp64Nano(end_time) AS end_ns, "
            "gen_ai_usage_input_tokens AS in_tok, gen_ai_usage_output_tokens AS out_tok, "
            "gen_ai_usage_cache_creation_input_tokens AS cache_write, "
            "gen_ai_usage_cache_read_input_tokens AS cache_read, cost_usd, "
            "span_attributes['vigil.retrieval.doc_ids'] AS doc_ids, "
            "span_attributes['vigil.retrieval.k'] AS retrieval_k, "
            "`events.name` AS event_names, `events.attributes` AS event_attrs "
            "FROM spans WHERE trace_id = {tid:String} ORDER BY start_time LIMIT {lim:UInt32}",
            {"tid": trace_id, "lim": _MAX_TRACE_SPANS},
        )
    except ClickHouseError as exc:
        raise HTTPException(502, f"clickhouse error: {exc}") from exc
    if not rows:
        raise HTTPException(404, "trace not found")
    # ClickHouse returns 64-bit ints as JSON strings (to preserve precision); coerce to int.
    for r in rows:
        r["start_ns"] = int(r["start_ns"])
        r["end_ns"] = int(r["end_ns"])
    root = min((r["start_ns"] for r in rows), default=0)
    is_llm = lambda r: bool(r["model"]) and r["span_name"].startswith("gen_ai")  # noqa: E731
    spans = [
        {
            "span_id": r["span_id"],
            "parent_span_id": r["parent_span_id"] or None,
            "name": r["span_name"],
            "kind": r["span_kind"],
            "role": r["role"] or None,
            "model": r["model"] or None,
            "start_ms": (r["start_ns"] - root) / 1e6,
            "duration_ms": (r["end_ns"] - r["start_ns"]) / 1e6,
            # Token/cost fields only where they apply (LLM calls); null elsewhere.
            "input_tokens": r["in_tok"] if is_llm(r) else None,
            "output_tokens": r["out_tok"] if is_llm(r) else None,
            "cache_write_tokens": r["cache_write"] if is_llm(r) else None,
            "cache_read_tokens": r["cache_read"] if is_llm(r) else None,
            "cost_usd": r["cost_usd"] if is_llm(r) else None,
            # Retrieval spans: the document ids/titles that were retrieved (not just the query).
            "retrieved_doc_ids": _json_list(r.get("doc_ids")),
            "retrieval_k": int(r["retrieval_k"]) if r.get("retrieval_k") else None,
            # Captured content parsed to its text + the recorded truncation flag.
            "events": [
                _parse_event(n, a)
                for n, a in zip(r.get("event_names") or [], r.get("event_attrs") or [])
            ],
        }
        for r in rows
    ]
    return {
        "trace_id": trace_id,
        "n_spans": len(spans),
        "truncated": len(spans) >= _MAX_TRACE_SPANS,
        "spans": spans,
    }


async def _load_series(run_id: uuid.UUID) -> dict[str, dict[str, list[float]]]:
    rows = await _fetchall(
        "SELECT case_id, passed, scores, cost_usd, latency_ms FROM eval_case_results "
        "WHERE run_id = %s AND status = 'ok'",
        (run_id,),
    )
    series: dict[str, dict[str, list[float]]] = {}

    def add(metric, case_id, value):
        series.setdefault(metric, {}).setdefault(case_id, []).append(float(value))

    for r in rows:
        add("passed", r["case_id"], 1.0 if r["passed"] else 0.0)
        for name, entry in (r["scores"] or {}).items():
            if isinstance(entry, dict) and isinstance(entry.get("score"), (int, float)):
                add(name, r["case_id"], entry["score"])
        if r["cost_usd"] is not None:
            add("cost_usd", r["case_id"], r["cost_usd"])
        if r["latency_ms"] is not None:
            add("latency_ms", r["case_id"], r["latency_ms"])
    return series


@app.get("/compare")
async def compare(
    baseline: uuid.UUID,
    candidate: uuid.UUID,
    alpha: float = Query(0.05, gt=0, lt=0.5),
    method: str = Query("t", pattern="^(t|percentile|bca)$"),
):
    b_meta = await _fetchone("SELECT suite_id FROM eval_runs WHERE id = %s", (baseline,))
    c_meta = await _fetchone("SELECT suite_id FROM eval_runs WHERE id = %s", (candidate,))
    if b_meta is None or c_meta is None:
        raise HTTPException(404, "run not found")
    if b_meta["suite_id"] != c_meta["suite_id"]:
        raise HTTPException(
            400,
            "runs are from different suites; a paired comparison requires the same suite",
        )
    b = await _load_series(baseline)
    c = await _load_series(candidate)
    shared_metrics = [m for m in b if m in c]
    # Paired case count: cases present (ok) in both runs, per metric they share overall.
    if shared_metrics:
        b_cases = set().union(*[set(b[m]) for m in shared_metrics])
        c_cases = set().union(*[set(c[m]) for m in shared_metrics])
        paired_cases = len(b_cases & c_cases)
    else:
        paired_cases = 0
    results = []
    for m in shared_metrics:
        v = paired_bootstrap_diff(
            b[m], c[m], metric=m, direction=METRIC_DIRECTION.get(m, HIGHER_IS_BETTER),
            threshold=_THRESHOLDS.get(m, 0.0), alpha=alpha, method=method,
        )
        results.append(
            {
                "metric": m,
                "gated": m in _GATE,
                "point_estimate": v.point_estimate,
                "ci_bound": v.ci_bound,
                "ci_excludes_zero": v.ci_excludes_zero,
                "beyond_threshold": v.beyond_threshold,
                "regressed": v.regressed,
                "direction": v.direction,
                "tail_prob": v.tail_prob,
                "n_questions": v.n_questions,
            }
        )
    return {
        "baseline": str(baseline),
        "candidate": str(candidate),
        "method": method,
        "alpha": alpha,
        "paired_cases": paired_cases,
        "gated_regression": any(r["regressed"] for r in results if r["gated"]),
        "metrics": results,
    }
