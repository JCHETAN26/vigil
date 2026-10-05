"""Report a τ²-bench retail eval run: success rate + pass^k, agent-vs-simulator cost split,
prompt-cache stats (from ClickHouse), and a Postgres↔ClickHouse cost-agreement check.

Reproducible per the CLAUDE.md bench convention: raw per-(task, trial) data + the computed
aggregate are saved as JSON, and the Markdown table is generated from that data. Reads the run's
scores/cost from Postgres and the per-LLM-call cache tokens from ClickHouse.

Usage:
    python bench/tau2_baseline.py --run <run_id> [--title "dev10x2"]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "engine"))

from hotpotqa_baseline import bootstrap_ci
from tau2_pass_k import pass_k_curve


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
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode().strip()


def load_units(dsn: str, run_id: str) -> tuple[dict, list[dict]]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        meta_row = conn.execute(
            "SELECT r.mode, r.trials_per_case, s.name FROM eval_runs r "
            "JOIN eval_suites s ON s.id = r.suite_id WHERE r.id = %s",
            (run_id,),
        ).fetchone()
        if meta_row is None:
            raise SystemExit(f"no run {run_id}")
        rows = conn.execute(
            "SELECT case_id, trial, status, passed, cost_usd, sim_cost_usd, input_tokens, "
            "output_tokens, sim_input_tokens, sim_output_tokens, output, trace_id "
            "FROM eval_case_results WHERE run_id = %s ORDER BY case_id, trial",
            (run_id,),
        ).fetchall()
    meta = {"run_id": run_id, "mode": meta_row[0], "trials_per_case": meta_row[1], "suite": meta_row[2]}
    units = []
    for (cid, trial, status, passed, cost, sim_cost, itok, otok, sitok, sotok, output, tid) in rows:
        info = (output or {}).get("info", {}) if isinstance(output, dict) else {}
        units.append(
            {
                "case_id": cid,
                "trial": trial,
                "status": status,
                "trace_id": tid,
                "passed": bool(passed),
                "reward": float(info.get("reward", 0.0)),
                "db_match": info.get("db_match"),
                "turns": info.get("turns"),
                "cost_usd": float(cost) if cost is not None else 0.0,
                "sim_cost_usd": float(sim_cost) if sim_cost is not None else 0.0,
                "input_tokens": itok or 0,
                "output_tokens": otok or 0,
                "sim_input_tokens": sitok or 0,
                "sim_output_tokens": sotok or 0,
            }
        )
    return meta, units


def cache_stats(run_id: str) -> dict:
    """Per-agent-LLM-call cache tokens from ClickHouse, and the overall cache hit rate."""
    raw = _ch(
        "SELECT gen_ai_usage_cache_creation_input_tokens, gen_ai_usage_cache_read_input_tokens, "
        "gen_ai_usage_input_tokens FROM spans "
        f"WHERE eval_run_id = '{run_id}' AND run_kind = 'eval' AND role = '' "
        "AND span_name LIKE 'gen_ai.chat%'"
    )
    calls = [[int(x) for x in line.split("\t")] for line in raw.splitlines() if line]
    write = sum(c[0] for c in calls)
    read = sum(c[1] for c in calls)
    uncached = sum(c[2] for c in calls)
    return {
        "n_agent_calls": len(calls),
        "cache_creation_tokens": write,
        "cache_read_tokens": read,
        "uncached_input_tokens": uncached,
        "calls_with_write": sum(1 for c in calls if c[0] > 0),
        "calls_with_read": sum(1 for c in calls if c[1] > 0),
        # Of the cacheable prefix accesses, the fraction served from cache (reads vs writes).
        "hit_rate": (read / (read + write)) if (read + write) > 0 else 0.0,
        "per_call": [{"cache_write": c[0], "cache_read": c[1], "uncached_input": c[2]} for c in calls],
    }


def cost_agreement(dsn: str, run_id: str, ok_trace_ids: list[str] | None = None) -> dict:
    import psycopg

    with psycopg.connect(dsn) as conn:
        pg_agent = float(
            conn.execute(
                "SELECT COALESCE(sum(cost_usd),0) FROM eval_case_results WHERE run_id=%s", (run_id,)
            ).fetchone()[0]
        )
        pg_sim = float(
            conn.execute(
                "SELECT COALESCE(sum(sim_cost_usd),0) FROM eval_case_results WHERE run_id=%s", (run_id,)
            ).fetchone()[0]
        )
    # Compare like-for-like: Postgres records cost only for OK units (failed units are $0), so
    # scope the ClickHouse side to those same OK traces. (The whole-run ClickHouse total also
    # includes spans from failed episodes — real spend Postgres doesn't attribute; reported
    # separately as spend_on_failures.)
    ok_traces = [t for t in (ok_trace_ids or []) if t]
    if ok_traces:
        tids = "','".join(ok_traces)
        ch_agent = float(
            _ch(
                f"SELECT sum(cost_usd) FROM spans WHERE trace_id IN ('{tids}') "
                "AND span_name LIKE 'gen_ai.chat%' AND role=''"
            )
            or 0
        )
        ch_sim = float(
            _ch(
                f"SELECT sum(cost_usd) FROM spans WHERE trace_id IN ('{tids}') "
                "AND span_name LIKE 'gen_ai.chat%' AND role='user_simulator'"
            )
            or 0
        )
    else:
        ch_agent = ch_sim = 0.0
    ch_run_total = float(
        _ch(f"SELECT sum(cost_usd) FROM spans WHERE eval_run_id='{run_id}' AND run_kind='eval'") or 0
    )
    return {
        "postgres_agent_usd": pg_agent,
        "clickhouse_agent_usd": ch_agent,
        "agent_agree": abs(pg_agent - ch_agent) < 1e-6,
        "postgres_sim_usd": pg_sim,
        "clickhouse_sim_usd": ch_sim,
        "sim_agree": abs(pg_sim - ch_sim) < 1e-6,
        # Real spend on episodes that failed (spans exist in ClickHouse but Postgres booked $0).
        "spend_on_failures_usd": max(0.0, ch_run_total - ch_agent - ch_sim),
    }


def aggregate(units: list[dict], *, n_boot: int, seed: int) -> dict:
    ok = [u for u in units if u["status"] == "ok"]
    # Per-task success counts for pass^k.
    by_case: dict[str, list[int]] = {}
    for u in ok:
        by_case.setdefault(u["case_id"], []).append(1 if u["passed"] else 0)
    successes = [(len(v), sum(v)) for v in by_case.values()]
    # Success rate with a question-level bootstrap CI (each task averages its trials first).
    per_task_rate = [sum(v) / len(v) for v in by_case.values()]
    agent_cost = sum(u["cost_usd"] for u in ok)
    sim_cost = sum(u["sim_cost_usd"] for u in ok)
    n_tasks = len(by_case)
    return {
        "n_tasks": n_tasks,
        "n_units": len(ok),
        "success_rate": bootstrap_ci(per_task_rate, n_boot=n_boot, seed=seed),
        "pass_k": pass_k_curve(successes),
        "cost": {
            "agent_usd": agent_cost,
            "sim_usd": sim_cost,
            "total_usd": agent_cost + sim_cost,
            "per_task_trial_usd": (agent_cost + sim_cost) / len(ok) if ok else None,
        },
    }


def _fmt(x, pct=False):
    if x is None:
        return "—"
    return f"{x * 100:.1f}%" if pct else f"{x:.4f}"


def render_markdown(meta, agg, cache, agree) -> str:
    L = []
    L.append(f"# τ²-bench retail baseline — {meta.get('title') or meta['suite']}")
    L.append("")
    L.append(
        f"_Run `{meta['run_id']}` · mode {meta['mode']} · {agg['n_tasks']} tasks × "
        f"{meta['trials_per_case']} trials = {agg['n_units']} units · reward = τ² DB-state match._"
    )
    L.append("")
    sr = agg["success_rate"]
    L.append(f"**Success rate (pass¹):** {_fmt(sr['mean'], pct=True)} "
             f"[{_fmt(sr['lo'], pct=True)}, {_fmt(sr['hi'], pct=True)}] (bootstrap over tasks)")
    L.append("")
    L.append("| pass^k | value |")
    L.append("|---|---|")
    for k in sorted(agg["pass_k"]):
        L.append(f"| pass^{k} | {_fmt(agg['pass_k'][k], pct=True)} |")
    L.append("")
    c = agg["cost"]
    L.append("**Cost:**")
    L.append("")
    L.append("| | USD |")
    L.append("|---|---|")
    L.append(f"| agent | {_fmt(c['agent_usd'])} |")
    L.append(f"| user simulator | {_fmt(c['sim_usd'])} |")
    L.append(f"| **total** | {_fmt(c['total_usd'])} |")
    L.append(f"| per task-trial | {_fmt(c['per_task_trial_usd'])} |")
    L.append("")
    L.append("**Prompt cache** (agent LLM calls):")
    L.append("")
    L.append(f"- calls: {cache['n_agent_calls']} · with cache-write: {cache['calls_with_write']} · "
             f"with cache-read: {cache['calls_with_read']}")
    L.append(f"- cache-creation tokens: {cache['cache_creation_tokens']:,} · "
             f"cache-read tokens: {cache['cache_read_tokens']:,} · "
             f"uncached input: {cache['uncached_input_tokens']:,}")
    L.append(f"- **cache hit rate** (read / (read+write)): {_fmt(cache['hit_rate'], pct=True)}")
    L.append("- This is Anthropic **prompt caching** (a cached prompt prefix, billed at the "
             "cache-read rate; responses are still generated), not Vigil's Redis response "
             "cache, which measurement mode refuses.")
    # Caching failed only if NEITHER writes nor reads happened. cache-creation can legitimately
    # be 0 when the prefix was already warm (written by a run within the 5-minute TTL) and every
    # call is a read — that is caching working, not failing.
    if cache["cache_creation_tokens"] == 0 and cache["cache_read_tokens"] == 0:
        L.append("")
        L.append("> ⚠️ **Prompt caching did NOT engage** (no cache-creation or cache-read "
                 "tokens). The cached prefix may be below Haiku's 4096-token minimum.")
    L.append("")
    L.append("**Cost agreement (Postgres vs ClickHouse, OK units):**")
    L.append(f"- agent: ${agree['postgres_agent_usd']:.6f} vs ${agree['clickhouse_agent_usd']:.6f} "
             f"→ {'MATCH' if agree['agent_agree'] else 'MISMATCH'}")
    L.append(f"- simulator: ${agree['postgres_sim_usd']:.6f} vs ${agree['clickhouse_sim_usd']:.6f} "
             f"→ {'MATCH' if agree['sim_agree'] else 'MISMATCH'}")
    if agree.get("spend_on_failures_usd", 0) > 1e-9:
        L.append(f"- spend on failed episodes (in ClickHouse, booked $0 in Postgres): "
                 f"${agree['spend_on_failures_usd']:.6f}")
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    from engine.config import pg_dsn

    p = argparse.ArgumentParser("tau2_baseline")
    p.add_argument("--run", required=True)
    p.add_argument("--out", default="bench/results/tau2")
    p.add_argument("--title", default=None)
    p.add_argument("--n-boot", type=int, default=10000)
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--dsn", default=None)
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    dsn = args.dsn or pg_dsn()
    meta, units = load_units(dsn, args.run)
    if args.title:
        meta["title"] = args.title
    if not any(u["status"] == "ok" for u in units):
        raise SystemExit("no ok units in this run")
    agg = aggregate(units, n_boot=args.n_boot, seed=args.seed)
    cache = cache_stats(args.run)
    ok_traces = [u["trace_id"] for u in units if u["status"] == "ok" and u.get("trace_id")]
    agree = cost_agreement(dsn, args.run, ok_traces)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        "meta": meta,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "aggregate": agg,
        "cache": cache,
        "cost_agreement": agree,
        "units": units,
    }
    (out_dir / f"{args.run}.json").write_text(json.dumps(raw, indent=2))
    md = render_markdown(meta, agg, cache, agree)
    (out_dir / f"{args.run}.md").write_text(md + "\n")
    print(md)
    print(f"\nraw data -> {out_dir / (args.run + '.json')}\ntable    -> {out_dir / (args.run + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
