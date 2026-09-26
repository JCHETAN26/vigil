"""Report a HotpotQA eval run as a reproducible baseline table.

Reads one ``eval_run``'s per-(case, trial) results from Postgres, **re-scores** each unit with
the current scorers (rebuilding a ``RunResult`` from the persisted transcript, so a run can be
re-scored after a scorer fix without paying to re-run the agent), and reports:

- **Question-level bootstrap CIs.** The statistic is the mean over questions, where each
  question first averages its trials. We resample *questions* (not trials) with replacement, so
  the CI reflects uncertainty from the finite question sample — the thing that generalizes.
- **Trial-to-trial variance, separately.** For each question with >1 trial we take the standard
  deviation across its trials, then report the mean (and max) of those per-question stds. This
  isolates run-to-run noise from question sampling, rather than conflating them.
- **Per-type breakdown** (bridge vs comparison), since the two ask different things of retrieval.
- **Cost and tokens.** Agent-only cost/tokens (retrieval/BM25 is free), summed and per question.

Outputs are reproducible per the ``CLAUDE.md`` bench convention: the raw per-unit data plus the
computed aggregation are saved as JSON, and the Markdown table is generated from that same data
(so the table can be regenerated without touching the DB). The bootstrap is seeded.

Usage:
    python bench/hotpotqa_baseline.py --run <run_id>
    python bench/hotpotqa_baseline.py --run <run_id> --out bench/results/hotpotqa --title "dev20"
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

# bench is tooling and may import the engine (only *agents* may not).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from engine.datasets.base import Case
from engine.scoring import score_case, scorers_for
from vigil import Retrieval, RunResult, ToolCall

# Metrics reported for HotpotQA. "passed" is the suite-decided pass flag; the rest are the
# per-scorer scores. Order is the table's row order.
METRICS = [
    "passed",
    "TokenF1",
    "ExactMatch",
    "RetrievalRecall@5",
    "RetrievalRecall@10",
    "NDCG",
    "AllGoldRetrieved",
]


# --- re-scoring ----------------------------------------------------------------------


def runresult_from_output(output: dict) -> RunResult:
    """Rebuild the ``RunResult`` a scorer needs from the persisted transcript (worker stores
    ``output``/``final_answer``/``tool_calls``/``retrievals``). Enough to re-run answer and
    retrieval scorers offline; token counts aren't needed for scoring so they stay 0."""
    return RunResult(
        output=output.get("output"),
        final_answer=output.get("final_answer"),
        tool_calls=[
            ToolCall(name=tc["name"], arguments=tc.get("arguments") or {})
            for tc in output.get("tool_calls", [])
        ],
        retrievals=[
            Retrieval(query=r.get("query", ""), doc_ids=list(r.get("doc_ids", [])))
            for r in output.get("retrievals", [])
        ],
    )


def rescore_unit(expected: dict, tags: list[str], output: dict) -> dict:
    """Re-score one unit with the current scorers and return a flat metric row: the pass flag
    plus each scorer's score, keyed by scorer name."""
    case = Case(case_id="", input=None, expected=expected, tags=tags)
    result = runresult_from_output(output)
    agg = score_case(case, result, scorers_for(expected))
    row = {"passed": 1.0 if agg.passed else 0.0}
    for name, entry in agg.scores.items():
        row[name] = float(entry["score"])
    return row


# --- pure aggregation (unit-tested) --------------------------------------------------


def per_question_means(units: list[dict]) -> dict[str, dict]:
    """Collapse per-(case, trial) rows to one row per question by averaging each metric over
    that question's trials. ``units`` are ``{"case_id", "type", "metrics": {name: value}}``.
    Returns ``{case_id: {"type", "n_trials", "means": {metric: mean}}}``."""
    by_case: dict[str, list[dict]] = {}
    types: dict[str, str] = {}
    for u in units:
        by_case.setdefault(u["case_id"], []).append(u["metrics"])
        types[u["case_id"]] = u["type"]
    out: dict[str, dict] = {}
    for case_id, rows in by_case.items():
        metrics = {k for r in rows for k in r}
        means = {m: statistics.fmean([r[m] for r in rows if m in r]) for m in metrics}
        out[case_id] = {"type": types[case_id], "n_trials": len(rows), "means": means}
    return out


def bootstrap_ci(
    values: list[float], *, n_boot: int = 10000, seed: int = 1234, alpha: float = 0.05
) -> dict:
    """Percentile bootstrap CI for the mean of ``values`` (one value per question). Resamples
    the questions with replacement ``n_boot`` times. Deterministic given ``seed``."""
    n = len(values)
    if n == 0:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    mean = statistics.fmean(values)
    if n == 1:
        return {"mean": mean, "lo": mean, "hi": mean, "n": 1}
    rng = random.Random(seed)
    boot_means = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        boot_means.append(statistics.fmean(sample))
    boot_means.sort()
    lo = boot_means[int((alpha / 2) * n_boot)]
    hi = boot_means[min(int((1 - alpha / 2) * n_boot), n_boot - 1)]
    return {"mean": mean, "lo": lo, "hi": hi, "n": n}


def trial_variance(units: list[dict], metric: str) -> dict:
    """Trial-to-trial variability for one metric: the standard deviation across a question's
    trials, averaged over questions that have >= 2 trials (and its max). ``None`` when no
    question has repeated trials (e.g. a single-trial dev run)."""
    by_case: dict[str, list[float]] = {}
    for u in units:
        if metric in u["metrics"]:
            by_case.setdefault(u["case_id"], []).append(u["metrics"][metric])
    stds = [statistics.pstdev(vs) for vs in by_case.values() if len(vs) >= 2]
    if not stds:
        return {"mean_std": None, "max_std": None, "n_questions": 0}
    return {
        "mean_std": statistics.fmean(stds),
        "max_std": max(stds),
        "n_questions": len(stds),
    }


def summarize(units: list[dict], metrics: list[str], *, n_boot: int, seed: int) -> dict:
    """Bootstrap CI + trial variance for each metric over the given units."""
    qmeans = per_question_means(units)
    out = {"n_questions": len(qmeans), "n_units": len(units), "metrics": {}}
    for m in metrics:
        vals = [q["means"][m] for q in qmeans.values() if m in q["means"]]
        out["metrics"][m] = {
            "ci": bootstrap_ci(vals, n_boot=n_boot, seed=seed),
            "trial_variance": trial_variance(units, m),
        }
    return out


def aggregate(units: list[dict], metrics: list[str], *, n_boot: int, seed: int) -> dict:
    """Overall + per-type (bridge/comparison) summaries, plus cost/token/latency totals."""
    result = {"overall": summarize(units, metrics, n_boot=n_boot, seed=seed), "by_type": {}}
    for typ in sorted({u["type"] for u in units}):
        subset = [u for u in units if u["type"] == typ]
        result["by_type"][typ] = summarize(subset, metrics, n_boot=n_boot, seed=seed)

    costs = [u["cost_usd"] for u in units]
    n_q = result["overall"]["n_questions"]
    result["cost"] = {
        "total_usd": sum(costs),
        "per_question_usd": (sum(costs) / n_q) if n_q else None,
        "mean_input_tokens": statistics.fmean([u["input_tokens"] for u in units]) if units else 0,
        "mean_output_tokens": statistics.fmean([u["output_tokens"] for u in units]) if units else 0,
        "mean_latency_ms": (
            statistics.fmean([u["latency_ms"] for u in units if u["latency_ms"] is not None])
            if any(u["latency_ms"] is not None for u in units)
            else None
        ),
    }
    return result


# --- DB ------------------------------------------------------------------------------


def load_units(dsn: str, run_id: str, *, rescore: bool) -> tuple[dict, list[dict]]:
    """Load a run's metadata and its per-(case, trial) units from Postgres. Each unit carries
    its metric row (re-scored from the transcript when ``rescore``, else the stored scores),
    its type, cost, tokens, and latency. Only ``status = 'ok'`` units are scored."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        meta_row = conn.execute(
            "SELECT r.suite_id, r.agent_id, r.mode, r.trials_per_case, r.cost_spent_usd, "
            "r.notes, s.name FROM eval_runs r JOIN eval_suites s ON s.id = r.suite_id "
            "WHERE r.id = %s",
            (run_id,),
        ).fetchone()
        if meta_row is None:
            raise SystemExit(f"no run {run_id}")
        meta = {
            "run_id": run_id,
            "suite": meta_row[6],
            "agent_id": meta_row[1],
            "mode": meta_row[2],
            "trials_per_case": meta_row[3],
            "cost_spent_usd": float(meta_row[4]) if meta_row[4] is not None else None,
            "notes": meta_row[5],
        }
        rows = conn.execute(
            "SELECT r.case_id, r.trial, r.status, r.passed, r.scores, r.output, "
            "r.input_tokens, r.output_tokens, r.cost_usd, r.latency_ms, ec.expected, ec.tags "
            "FROM eval_case_results r JOIN eval_cases ec "
            "  ON ec.suite_id = %s AND ec.case_id = r.case_id "
            "WHERE r.run_id = %s ORDER BY r.case_id, r.trial",
            (meta_row[0], run_id),
        ).fetchall()

    units: list[dict] = []
    skipped = 0
    for case_id, trial, status, passed, scores, output, in_tok, out_tok, cost, lat, expected, tags in rows:
        if status != "ok":
            skipped += 1
            continue
        typ = expected.get("type") or ("comparison" if "comparison" in (tags or []) else "bridge")
        if rescore:
            if not output:
                raise SystemExit(
                    f"unit {case_id}/{trial} has no persisted transcript to re-score; "
                    "run with --stored-scores to use the scores recorded at run time"
                )
            metrics = rescore_unit(expected, list(tags or []), output)
        else:
            metrics = {"passed": 1.0 if passed else 0.0}
            for name, entry in (scores or {}).items():
                metrics[name] = float(entry["score"])
        units.append(
            {
                "case_id": case_id,
                "trial": trial,
                "type": typ,
                "metrics": metrics,
                "cost_usd": float(cost) if cost is not None else 0.0,
                "input_tokens": in_tok or 0,
                "output_tokens": out_tok or 0,
                "latency_ms": lat,
            }
        )
    meta["skipped_non_ok"] = skipped
    return meta, units


# --- rendering -----------------------------------------------------------------------


def _fmt(x, pct=False):
    if x is None:
        return "—"
    return f"{x * 100:.1f}%" if pct else f"{x:.3f}"


def _metric_line(name: str, summary: dict) -> str:
    ms = summary["metrics"].get(name)
    if ms is None or ms["ci"]["mean"] is None:
        return f"| {name} | — | — | — |"
    ci = ms["ci"]
    tv = ms["trial_variance"]
    tvs = _fmt(tv["mean_std"]) if tv["mean_std"] is not None else "—"
    return (
        f"| {name} | {_fmt(ci['mean'])} | [{_fmt(ci['lo'])}, {_fmt(ci['hi'])}] | {tvs} |"
    )


def render_markdown(meta: dict, agg: dict, metrics: list[str]) -> str:
    lines: list[str] = []
    lines.append(f"# HotpotQA baseline — {meta.get('title') or meta['suite']}")
    lines.append("")
    n_q = agg["overall"]["n_questions"]
    cost = agg["cost"]
    n_units = agg["overall"]["n_units"]
    trials = meta["trials_per_case"]
    lines.append(
        f"_Run `{meta['run_id']}` · agent `{meta['agent_id']}` · mode {meta['mode']} · "
        f"{n_q} questions × {trials} trial(s) = {n_units} units._"
    )
    lines.append("")
    lines.append(
        f"**Cost:** ${cost['total_usd']:.4f} total, ${cost['per_question_usd']:.4f}/question · "
        f"mean tokens {cost['mean_input_tokens']:.0f} in / {cost['mean_output_tokens']:.0f} out"
        + (f" · mean latency {cost['mean_latency_ms']:.0f} ms" if cost["mean_latency_ms"] else "")
    )
    lines.append("")

    def block(title: str, summary: dict) -> None:
        lines.append(f"### {title} (n={summary['n_questions']})")
        lines.append("")
        lines.append(
            "| Metric | Mean | 95% CI (bootstrap over questions) | Trial σ (mean/question) |"
        )
        lines.append("|---|---|---|---|")
        for m in metrics:
            lines.append(_metric_line(m, summary))
        lines.append("")

    block("Overall", agg["overall"])
    for typ, summary in agg["by_type"].items():
        block(f"Type: {typ}", summary)

    tv_note = agg["overall"]["metrics"]["TokenF1"]["trial_variance"]
    if tv_note["mean_std"] is None:
        lines.append(
            "_Trial σ is —: this run has a single trial per question, so trial-to-trial "
            "variance is not defined. The 100×3 baseline reports it._"
        )
        lines.append("")
    return "\n".join(lines)


# --- main ----------------------------------------------------------------------------


def main(argv=None) -> int:
    from engine.config import pg_dsn

    p = argparse.ArgumentParser("hotpotqa_baseline")
    p.add_argument("--run", required=True, help="eval_run id to report on")
    p.add_argument("--out", default="bench/results/hotpotqa", help="output directory")
    p.add_argument("--title", default=None, help="human label for the table heading")
    p.add_argument("--n-boot", type=int, default=10000, help="bootstrap resamples")
    p.add_argument("--seed", type=int, default=1234, help="bootstrap seed (reproducible CIs)")
    p.add_argument(
        "--stored-scores",
        action="store_true",
        help="use the scores recorded at run time instead of re-scoring the transcript",
    )
    p.add_argument("--dsn", default=None, help="Postgres DSN (default: from env)")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    dsn = args.dsn or pg_dsn()
    meta, units = load_units(dsn, args.run, rescore=not args.stored_scores)
    if not units:
        raise SystemExit("no scorable (status=ok) units in this run")
    if args.title:
        meta["title"] = args.title
    meta["rescored"] = not args.stored_scores

    metrics = [m for m in METRICS if any(m in u["metrics"] for u in units)]
    agg = aggregate(units, metrics, n_boot=args.n_boot, seed=args.seed)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        "meta": meta,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_boot": args.n_boot,
        "seed": args.seed,
        "metrics": metrics,
        "units": units,
        "aggregate": agg,
    }
    raw_path = out_dir / f"{args.run}.json"
    raw_path.write_text(json.dumps(raw, indent=2))

    md = render_markdown(meta, agg, metrics)
    md_path = out_dir / f"{args.run}.md"
    md_path.write_text(md + "\n")

    print(md)
    print(f"\nraw data -> {raw_path}\ntable    -> {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
