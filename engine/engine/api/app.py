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
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from engine import gate
from engine.config import dashboard_origins, pg_ro_dsn
from engine.db.triage import PgTriageStore
from engine.manifests import REDACTION_NOTE, diff_manifests, sanitize_manifest
from engine.regression import StatsDependencyError

from .clickhouse import ClickHouseClient, ClickHouseError

TraceId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
_MAX_TRACE_SPANS = 5000
_VERSION_PATTERN = r"^[0-9a-f]{8,64}$"


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = AsyncConnectionPool(
        pg_ro_dsn(), min_size=1, max_size=8, open=False, kwargs={"row_factory": dict_row}
    )
    await pool.open()
    app.state.pool = pool
    app.state.ch = ClickHouseClient()
    app.state.triage = PgTriageStore(_fetchall)
    try:
        yield
    finally:
        await pool.close()


def add_cors(target: FastAPI, origins: list[str]) -> None:
    """Exact-origin, read-only CORS: GET only, no credentials, never a wildcard origin."""
    target.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )


app = FastAPI(title="Vigil dashboard API", lifespan=lifespan)
# VIGIL_DASHBOARD_ORIGINS (default: localhost/127.0.0.1 :3000); malformed values fail startup.
add_cors(app, dashboard_origins())


@app.exception_handler(StatsDependencyError)
async def _missing_stats_dependency(_request, exc: StatsDependencyError):
    # A broken install, not a verdict: 503 with the actionable message (never an approximation).
    return JSONResponse(status_code=503, content={"detail": str(exc)})


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
        "r.cost_spent_usd, r.created_at, r.notes "
        "FROM eval_runs r JOIN eval_suites s ON s.id = r.suite_id "
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
        # Free text recorded with the run (e.g. the SYNTHETIC label on the demo fixture's runs).
        "notes": r.get("notes"),
    }


@app.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID):
    meta = await _fetchone(
        "SELECT r.id, s.name AS suite, r.agent_id, r.mode, r.trials_per_case, r.status, "
        "r.cost_spent_usd, r.created_at, r.notes "
        "FROM eval_runs r JOIN eval_suites s ON s.id = r.suite_id WHERE r.id = %s",
        (run_id,),
    )
    if meta is None:
        raise HTTPException(404, "run not found")
    agg = await _fetchone(
        "SELECT count(*) AS n, count(*) FILTER (WHERE status='ok') AS n_ok, "
        "avg((passed)::int) FILTER (WHERE status='ok') AS pass_rate, "
        "avg(cost_usd) FILTER (WHERE status='ok') AS mean_cost, "
        "avg(latency_ms) FILTER (WHERE status='ok') AS mean_latency, "
        "count(DISTINCT agent_version) AS n_versions "
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
        # The aggregates above pool every version's results; >1 means they are not per-version.
        "n_versions": agg["n_versions"],
    }


@app.get("/runs/{run_id}/versions")
async def get_run_versions(
    run_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """The agent versions a run holds, for choosing one per comparison side: every registered
    version (even one with no results yet) plus any version that only appears in results.
    Full hashes, ordered by hash; the order is not a baseline/candidate suggestion."""
    store = app.state.triage
    if await store.get_run(str(run_id)) is None:
        raise HTTPException(404, "run not found")
    rows, total = await store.get_run_versions_page(str(run_id), limit, offset)
    return {
        "run_id": str(run_id),
        "versions": [
            {
                "agent_version": r["agent_version"],
                # eval_run_versions.status is NOT NULL, so NULL marks the results-only fallback.
                "registered": r["status"] is not None,
                "status": r["status"],
                "cases_total": r["cases_total"],
                "cases_done": r["cases_done"],
            }
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
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
        "SELECT r.agent_version, r.case_id, r.trial, r.status, r.passed, r.score, r.scores, "
        "r.cost_usd, r.input_tokens, r.output_tokens, r.latency_ms, r.trace_id, ec.tags "
        "FROM eval_case_results r JOIN eval_runs run ON run.id = r.run_id "
        "JOIN eval_cases ec ON ec.suite_id = run.suite_id AND ec.case_id = r.case_id "
        # A multi-version run holds each (case_id, trial) once per version. Ordering by the whole
        # UNIQUE (run_id, agent_version, case_id, trial) key keeps LIMIT/OFFSET pages stable.
        "WHERE r.run_id = %s ORDER BY r.case_id, r.agent_version, r.trial LIMIT %s OFFSET %s",
        (run_id, limit, offset),
    )
    return {
        "run_id": str(run_id),
        "limit": limit,
        "offset": offset,
        "cases": [
            {
                "agent_version": r["agent_version"],
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
        # The ClickHouse client is blocking urllib (up to its timeout); keep it off the event loop
        # so a slow or unreachable ClickHouse cannot stall every other request.
        rows = await run_in_threadpool(
            app.state.ch.query,
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


# --- regression triage (engine.gate; contract in engine/TRIAGE_API.md) ---------------


@dataclass
class _Pair:
    baseline: uuid.UUID
    candidate: uuid.UUID
    baseline_version: str | None
    candidate_version: str | None


def _pair(
    baseline: uuid.UUID,
    candidate: uuid.UUID,
    baseline_version: str | None = Query(None, pattern=_VERSION_PATTERN),
    candidate_version: str | None = Query(None, pattern=_VERSION_PATTERN),
) -> _Pair:
    return _Pair(baseline, candidate, baseline_version, candidate_version)


def _policy(
    alpha: float = Query(gate.DEFAULT_POLICY.alpha, gt=0, lt=0.5),
    method: str = Query(gate.DEFAULT_POLICY.method, pattern="^(t|percentile|bca)$"),
    threshold_pass: float | None = Query(None, ge=0, le=1),
    threshold_f1: float | None = Query(None, ge=0, le=1),
    n_boot: int = Query(gate.DEFAULT_POLICY.n_boot, ge=1000, le=20000),
    seed: int = Query(gate.DEFAULT_POLICY.seed, ge=0, le=2**32 - 1),
    two_level: bool = Query(False),
) -> gate.Policy:
    try:
        return gate.DEFAULT_POLICY.with_overrides(
            alpha=alpha, method=method, threshold_pass=threshold_pass,
            threshold_f1=threshold_f1, n_boot=n_boot, seed=seed, two_level=two_level,
        )
    except gate.ComparisonError as exc:
        raise HTTPException(422, str(exc)) from exc


def _http(exc: gate.ComparisonError) -> HTTPException:
    return HTTPException(404 if exc.kind == "not_found" else 400, str(exc))


async def _inputs(p: _Pair) -> gate.ComparisonInputs:
    try:
        return await gate.load_inputs(
            app.state.triage, str(p.baseline), str(p.candidate),
            p.baseline_version, p.candidate_version,
        )
    except gate.ComparisonError as exc:
        raise _http(exc) from exc


def _summary_fields(cmp: gate.Comparison) -> dict:
    d = cmp.to_dict()
    return {
        "baseline_version": cmp.baseline.agent_version,
        "candidate_version": cmp.candidate.agent_version,
        "verdict": d["verdict"],
        "complete": d["complete"],
        "incomplete_reasons": d["incomplete_reasons"],
        "context": {"baseline": d["baseline"], "candidate": d["candidate"]},
        "policy": d["policy"],
        "coverage": d["coverage"],
    }


@app.get("/compare")
async def compare(
    pair: Annotated[_Pair, Depends(_pair)], policy: Annotated[gate.Policy, Depends(_policy)]
):
    # The bootstrap is CPU-bound (n_boot × metrics); keep it off the event loop.
    cmp = await run_in_threadpool(gate.compare, await _inputs(pair), policy)
    d = cmp.to_dict()
    return gate.json_safe({
        # Pre-triage response fields, unchanged in shape.
        "baseline": str(pair.baseline),
        "candidate": str(pair.candidate),
        "method": policy.method,
        "alpha": policy.alpha,
        "paired_cases": cmp.paired_cases,
        "gated_regression": cmp.gated_regression,
        "metrics": d["metrics"],
        # Resolved context, effective policy, coverage audit, metrics that could not be compared.
        **_summary_fields(cmp),
        "skipped_metrics": d["skipped_metrics"],
    })


_CASE_FILTERS = "^(all|adverse|unpaired|failed)$"


@app.get("/compare/cases")
async def compare_cases(
    pair: Annotated[_Pair, Depends(_pair)],
    policy: Annotated[gate.Policy, Depends(_policy)],
    metric: str | None = Query(None, pattern=r"^[A-Za-z0-9_@.\-]{1,64}$"),
    only: str = Query("all", alias="filter", pattern=_CASE_FILTERS),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    inputs = await _inputs(pair)
    available = gate.available_metrics(inputs)
    # No results on either side → `passed` with an empty case list (200, verdict incomplete).
    sort_metric = metric or next((m for m in policy.gated if m in available), available[0]
                                 if available else "passed")
    try:
        rows = gate.case_deltas(inputs, sort_metric)
    except gate.ComparisonError as exc:
        raise _http(exc) from exc
    if only == "adverse":
        rows = [r for r in rows if (r["adverse_delta"] or 0.0) > 0]
    elif only == "unpaired":
        rows = [r for r in rows if not r["paired"]]
    elif only == "failed":
        rows = [r for r in rows if r["baseline"]["n_failed"] or r["candidate"]["n_failed"]]
    cmp = await run_in_threadpool(gate.compare, inputs, policy)  # CPU-bound bootstrap
    return gate.json_safe({
        "baseline": str(pair.baseline),
        "candidate": str(pair.candidate),
        **_summary_fields(cmp),
        "sort_metric": sort_metric,
        "sort_direction": gate.direction_of(sort_metric),
        "metrics_available": available,
        "filter": only,
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "cases": rows[offset: offset + limit],
    })


@app.get("/versions/diff")
async def versions_diff(pair: Annotated[_Pair, Depends(_pair)]):
    try:
        b, c = await gate.resolve_pair(
            app.state.triage, str(pair.baseline), str(pair.candidate),
            pair.baseline_version, pair.candidate_version,
            require_same_suite=False, allow_identical=True,
        )
    except gate.ComparisonError as exc:
        raise _http(exc) from exc
    raw_b = await app.state.triage.get_manifest(b.agent_id, b.agent_version)
    raw_c = await app.state.triage.get_manifest(c.agent_id, c.agent_version)
    man_b, red_b, trunc_b = sanitize_manifest(raw_b)
    man_c, red_c, trunc_c = sanitize_manifest(raw_c)
    fields = diff_manifests(raw_b, raw_c)
    missing = [s for s, m in (("baseline", raw_b), ("candidate", raw_c)) if m is None]
    return {
        "baseline": {"run_id": b.run_id, "agent_id": b.agent_id,
                     "agent_version": b.agent_version, "run_git_sha": b.git_sha,
                     "version_selection": b.version_selection, "suite_id": b.suite_id},
        "candidate": {"run_id": c.run_id, "agent_id": c.agent_id,
                      "agent_version": c.agent_version, "run_git_sha": c.git_sha,
                      "version_selection": c.version_selection, "suite_id": c.suite_id},
        "same_suite": b.suite_id == c.suite_id,
        "same_version": b.agent_version == c.agent_version,
        "identical": None if missing else not any(f["changed"] for f in fields.values()),
        "missing_manifests": missing,
        "fields": fields,
        "baseline_manifest": man_b,
        "candidate_manifest": man_c,
        "redaction": {
            "redacted_paths": {"baseline": red_b, "candidate": red_c},
            "truncated_paths": {"baseline": trunc_b, "candidate": trunc_c},
            "note": REDACTION_NOTE,
        },
    }
