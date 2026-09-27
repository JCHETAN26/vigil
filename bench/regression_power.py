"""Validate the regression detector's power and false-positive rate on the HotpotQA baseline.

Per the Week-3 design review: do NOT perturb a fixed baseline. Instead fit a per-question model
from the real baseline's trials (each question's success probability), then in every simulation
draw a FRESH baseline and a FRESH candidate independently from that model — so both runs carry
realistic run-to-run noise — and apply the injected shift only to the candidate. Shifts are
applied either uniformly (all questions) or concentrated on a subset (e.g. bridge questions).

Outputs (reproducible, seeded): a detection-power curve vs effect size, the false-positive rate
at zero effect (should ≈ α), and the **minimum detectable effect** — the smallest shift detected
at 80% power for this suite size — used to confirm the practical thresholds sit at or above it.

The detector under test is engine.regression.paired_bootstrap_diff (the same code the CLI uses);
only the synthetic data generation lives here.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from engine.regression import HIGHER_IS_BETTER, paired_bootstrap_diff


def simulate_pass_run(p: np.ndarray, n_trials: int, rng, shift: float, shift_mask: np.ndarray):
    """Draw one run's per-question pass outcomes from the model. Each question q yields n_trials
    Bernoulli draws at probability p_q, reduced by `shift` (clamped to [0,1]) where shift_mask is
    True. Returns {case_id: [0/1, ...]} keyed by question index."""
    probs = np.where(shift_mask, np.clip(p - shift, 0.0, 1.0), p)
    draws = (rng.random((len(p), n_trials)) < probs[:, None]).astype(float)
    return {str(i): draws[i].tolist() for i in range(len(p))}


def detection_rate(
    p, *, n_trials, delta, shift_mask, n_sims, alpha, n_boot, seed, threshold=0.0, gated=False
) -> float:
    """Fraction of simulations flagged when the candidate is shifted by `delta` on shift_mask.
    By default counts the statistical part alone (one-sided CI excludes zero) — used for power
    and MDE. With `gated=True` it counts the full two-part rule (CI excludes zero AND point
    estimate ≥ threshold). With delta=0 the result is a false-positive rate."""
    ss = np.random.SeedSequence(seed)
    child = ss.spawn(n_sims)
    flagged = 0
    for i in range(n_sims):
        rng = np.random.default_rng(child[i])
        base = simulate_pass_run(p, n_trials, rng, 0.0, shift_mask)  # baseline: no shift
        cand = simulate_pass_run(p, n_trials, rng, delta, shift_mask)  # candidate: shifted
        v = paired_bootstrap_diff(
            base, cand, metric="passed", direction=HIGHER_IS_BETTER,
            threshold=threshold, alpha=alpha, n_boot=n_boot, seed=int(rng.integers(1 << 31)),
        )
        flagged += int(v.regressed if gated else v.ci_excludes_zero)
    return flagged / n_sims


def power_curve(p, *, deltas, shift_mask, n_trials, n_sims, alpha, n_boot, seed) -> dict:
    return {
        round(float(d), 4): detection_rate(
            p, n_trials=n_trials, delta=d, shift_mask=shift_mask,
            n_sims=n_sims, alpha=alpha, n_boot=n_boot, seed=seed + i,
        )
        for i, d in enumerate(deltas)
    }


def min_detectable_effect(curve: dict, target: float = 0.80) -> float | None:
    """Smallest effect size reaching `target` power, linearly interpolated between grid points.
    None if the curve never reaches target within the grid."""
    pts = sorted(curve.items())
    for (d0, r0), (d1, r1) in itertools.pairwise(pts):
        if r0 < target <= r1:
            if r1 == r0:
                return d1
            return d0 + (target - r0) * (d1 - d0) / (r1 - r0)
    if pts and pts[0][1] >= target:
        return pts[0][0]
    return None


# --- baseline model loading (Postgres) ----------------------------------------------


def load_pass_model(dsn: str, run_id: str) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Fit the per-question pass model from a real run: p_q = fraction of that question's trials
    that passed. Also return a boolean mask of 'bridge'-type questions (for concentrated shifts)."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT r.case_id, r.passed, ec.tags FROM eval_case_results r "
            "JOIN eval_runs run ON run.id = r.run_id "
            "JOIN eval_cases ec ON ec.suite_id = run.suite_id AND ec.case_id = r.case_id "
            "WHERE r.run_id = %s AND r.status = 'ok'",
            (run_id,),
        ).fetchall()
    by_case: dict[str, list[float]] = {}
    tags: dict[str, list[str]] = {}
    for case_id, passed, t in rows:
        by_case.setdefault(case_id, []).append(1.0 if passed else 0.0)
        tags[case_id] = t or []
    cases = sorted(by_case)
    p = np.array([float(np.mean(by_case[c])) for c in cases])
    bridge = np.array(["bridge" in tags[c] for c in cases])
    return p, bridge, cases


def main(argv=None) -> int:
    from engine.config import pg_dsn

    ap = argparse.ArgumentParser("regression_power")
    ap.add_argument("--run", required=True, help="baseline run id to fit the model from")
    ap.add_argument("--out", default="bench/results/regression")
    ap.add_argument("--n-sims", type=int, default=300)
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--threshold-pass", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    p, bridge, _cases = load_pass_model(args.dsn or pg_dsn(), args.run)
    n_trials = 3
    deltas = [0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20]
    all_mask = np.ones(len(p), dtype=bool)

    uniform = power_curve(
        p, deltas=deltas, shift_mask=all_mask, n_trials=n_trials,
        n_sims=args.n_sims, alpha=args.alpha, n_boot=args.n_boot, seed=args.seed,
    )
    bridge_only = power_curve(
        p, deltas=deltas, shift_mask=bridge, n_trials=n_trials,
        n_sims=args.n_sims, alpha=args.alpha, n_boot=args.n_boot, seed=args.seed + 1000,
    )
    mde_uniform = min_detectable_effect(uniform)
    fpr = uniform[0.0]  # CI-part false-positive rate (statistical only)
    gated_fpr = detection_rate(  # full two-part rule (CI + threshold) at zero effect
        p, n_trials=n_trials, delta=0.0, shift_mask=all_mask, n_sims=args.n_sims,
        alpha=args.alpha, n_boot=args.n_boot, seed=args.seed, threshold=args.threshold_pass, gated=True,
    )

    result = {
        "meta": {
            "baseline_run": args.run, "n_questions": len(p), "n_bridge": int(bridge.sum()),
            "n_trials": n_trials, "alpha": args.alpha, "n_sims": args.n_sims,
            "n_boot": args.n_boot, "seed": args.seed, "baseline_pass_rate": float(p.mean()),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "power_uniform": uniform,
        "power_bridge_only": bridge_only,
        "false_positive_rate_ci_part": fpr,
        "false_positive_rate_gated_rule": gated_fpr,
        "min_detectable_effect_uniform": mde_uniform,
        "threshold_pass": args.threshold_pass,
        "threshold_at_or_above_mde": (mde_uniform is not None and args.threshold_pass >= mde_uniform),
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.run}.json").write_text(json.dumps(result, indent=2))

    lines = [
        f"# Regression detector — power & MDE on HotpotQA baseline `{args.run[:12]}`",
        "",
        (
            f"_Model fit from {len(p)} questions × {n_trials} trials (pass rate "
            f"{p.mean() * 100:.1f}%, {int(bridge.sum())} bridge). Each simulation draws a fresh "
            f"baseline and candidate; the shift is applied only to the candidate. "
            f"α={args.alpha} one-sided, {args.n_sims} sims/point, {args.n_boot} bootstraps._"
        ),
        "",
        "| effect (Δ pass rate) | power (uniform) | power (bridge-only) |",
        "|---|---|---|",
    ]
    for d in deltas:
        lines.append(f"| {d:.2f} | {uniform[round(d, 4)] * 100:.0f}% | {bridge_only[round(d, 4)] * 100:.0f}% |")
    lines += [
        "",
        (
            f"- **False-positive rate** (Δ=0): CI-part {fpr * 100:.1f}% "
            f"(target ≈ {args.alpha * 100:.0f}%); full gated rule {gated_fpr * 100:.1f}% "
            "(the threshold suppresses the CI part's residual false positives)"
        ),
        "- **Minimum detectable effect** (uniform, 80% power): "
        + (f"{mde_uniform * 100:.1f}% pass-rate drop" if mde_uniform is not None else "not reached in grid"),
        f"- **Practical threshold** (pass rate): {args.threshold_pass * 100:.1f}% — "
        + ("**at or above** the MDE ✓" if result["threshold_at_or_above_mde"] else "**below** the MDE ⚠️"),
        "",
    ]
    (out / f"{args.run}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nraw -> {out / (args.run + '.json')}\ntable -> {out / (args.run + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
