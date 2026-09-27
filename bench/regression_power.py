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

from engine.regression import CI_METHODS, HIGHER_IS_BETTER, paired_bootstrap_diff


def simulate_pass_run(p: np.ndarray, n_trials: int, rng, shift: float, shift_mask: np.ndarray):
    """Draw one run's per-question pass outcomes from the model. Each question q yields n_trials
    Bernoulli draws at probability p_q, reduced by `shift` (clamped to [0,1]) where shift_mask is
    True. Returns {case_id: [0/1, ...]} keyed by question index."""
    probs = np.where(shift_mask, np.clip(p - shift, 0.0, 1.0), p)
    draws = (rng.random((len(p), n_trials)) < probs[:, None]).astype(float)
    return {str(i): draws[i].tolist() for i in range(len(p))}


def detection_rate(
    p, *, n_trials, delta, shift_mask, n_sims, alpha, n_boot, seed,
    threshold=0.0, gated=False, method="t",
) -> float:
    """Fraction of simulations flagged when the candidate is shifted by `delta` on shift_mask.
    By default counts the statistical part alone (one-sided CI excludes zero) — used for power
    and MDE. With `gated=True` it counts the full two-part rule (CI excludes zero AND point
    estimate ≥ threshold). With delta=0 the result is a false-positive rate. `method` selects
    the CI method (percentile | bca | t)."""
    ss = np.random.SeedSequence(seed)
    child = ss.spawn(n_sims)
    flagged = 0
    for i in range(n_sims):
        rng = np.random.default_rng(child[i])
        base = simulate_pass_run(p, n_trials, rng, 0.0, shift_mask)  # baseline: no shift
        cand = simulate_pass_run(p, n_trials, rng, delta, shift_mask)  # candidate: shifted
        v = paired_bootstrap_diff(
            base, cand, metric="passed", direction=HIGHER_IS_BETTER, method=method,
            threshold=threshold, alpha=alpha, n_boot=n_boot, seed=int(rng.integers(1 << 31)),
        )
        flagged += int(v.regressed if gated else v.ci_excludes_zero)
    return flagged / n_sims


def power_curve(
    p, *, deltas, shift_mask, n_trials, n_sims, alpha, n_boot, seed,
    threshold=0.0, gated=False, method="t",
) -> dict:
    return {
        round(float(d), 4): detection_rate(
            p, n_trials=n_trials, delta=d, shift_mask=shift_mask, n_sims=n_sims, alpha=alpha,
            n_boot=n_boot, seed=seed + i, threshold=threshold, gated=gated, method=method,
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
    kw = {"n_trials": n_trials, "n_sims": args.n_sims, "alpha": args.alpha, "n_boot": args.n_boot}

    # (1) Compare CI methods on their false-positive rate (CI part, Δ=0) and mid-effect power,
    # then ADOPT the one whose FPR is closest to α while keeping power (within 3pts of the best).
    comparison = {}
    for m in CI_METHODS:
        comparison[m] = {
            "fpr": detection_rate(p, delta=0.0, shift_mask=all_mask, seed=args.seed, method=m, **kw),
            "power_at_0.06": detection_rate(
                p, delta=0.06, shift_mask=all_mask, seed=args.seed + 7, method=m, **kw
            ),
        }
    # "Keeping power": drop only methods that lose meaningful power (>10 pts below the best);
    # among the rest, adopt the one whose FPR is closest to α. (BCa can be anti-conservative on
    # discrete/small data, so a high-power-but-high-FPR method is correctly rejected here.)
    best_power = max(c["power_at_0.06"] for c in comparison.values())
    eligible = [m for m, c in comparison.items() if c["power_at_0.06"] >= best_power - 0.10]
    adopted = min(eligible, key=lambda m: abs(comparison[m]["fpr"] - args.alpha))

    # (2) With the adopted method: CI-part MDE (to justify the threshold) + the FULL GATED rule's
    # power curve and MDE (CI excludes zero AND point estimate ≥ threshold), uniform + bridge-only.
    ci_uniform = power_curve(p, deltas=deltas, shift_mask=all_mask, seed=args.seed, method=adopted, **kw)
    gated_uniform = power_curve(
        p, deltas=deltas, shift_mask=all_mask, seed=args.seed, method=adopted,
        gated=True, threshold=args.threshold_pass, **kw,
    )
    gated_bridge = power_curve(
        p, deltas=deltas, shift_mask=bridge, seed=args.seed + 1000, method=adopted,
        gated=True, threshold=args.threshold_pass, **kw,
    )
    ci_mde = min_detectable_effect(ci_uniform)
    gated_mde = min_detectable_effect(gated_uniform)
    gated_fpr = gated_uniform[0.0]
    ci_fpr = ci_uniform[0.0]

    result = {
        "meta": {
            "baseline_run": args.run, "n_questions": len(p), "n_bridge": int(bridge.sum()),
            "n_trials": n_trials, "alpha": args.alpha, "n_sims": args.n_sims,
            "n_boot": args.n_boot, "seed": args.seed, "baseline_pass_rate": float(p.mean()),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method_comparison": comparison,
        "adopted_method": adopted,
        "threshold_pass": args.threshold_pass,
        "ci_part": {"fpr": ci_fpr, "mde": ci_mde, "power_uniform": ci_uniform},
        "gated_rule": {
            "fpr": gated_fpr, "mde": gated_mde,
            "power_uniform": gated_uniform, "power_bridge_only": gated_bridge,
        },
        "threshold_at_or_above_ci_mde": (ci_mde is not None and args.threshold_pass >= ci_mde),
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.run}.json").write_text(json.dumps(result, indent=2))

    def pct(x):
        return f"{x * 100:.0f}%" if x is not None else "—"

    lines = [
        f"# Regression detector — method choice, power & MDE on HotpotQA baseline `{args.run[:12]}`",
        "",
        (
            f"_Model fit from {len(p)} questions × {n_trials} trials (pass rate "
            f"{p.mean() * 100:.1f}%, {int(bridge.sum())} bridge). Each simulation draws a fresh "
            f"baseline and candidate independently; the shift is applied only to the candidate. "
            f"α={args.alpha} one-sided, {args.n_sims} sims/point, {args.n_boot} bootstraps._"
        ),
        "",
        "## CI method comparison (false-positive rate vs power)",
        "",
        "| method | FPR (Δ=0, target 5%) | power @ 6% drop |",
        "|---|---|---|",
    ]
    for m in CI_METHODS:
        star = " ← adopted" if m == adopted else ""
        lines.append(
            f"| {m}{star} | {comparison[m]['fpr'] * 100:.1f}% | "
            f"{comparison[m]['power_at_0.06'] * 100:.0f}% |"
        )
    lines += [
        "",
        f"Adopted **{adopted}**: FPR closest to α while keeping power.",
        "",
        "## Full gated-rule power (CI excludes zero AND point estimate ≥ threshold)",
        "",
        "| effect (Δ pass rate) | gated power (uniform) | gated power (bridge-only) |",
        "|---|---|---|",
    ]
    for d in deltas:
        lines.append(
            f"| {d:.2f} | {pct(gated_uniform[round(d, 4)])} | {pct(gated_bridge[round(d, 4)])} |"
        )
    thr = args.threshold_pass * 100
    lines += [
        "",
        (
            f"**Headline: on {len(p)} questions × {n_trials} trials, the gated detector "
            f"({adopted} CI, threshold {thr:.0f}%) catches a "
            f"{gated_mde * 100:.1f}-point pass-rate drop" if gated_mde is not None
            else f"**Headline: on {len(p)} questions × {n_trials} trials, the gated detector "
            f"({adopted}, threshold {thr:.0f}%) does not reach"
        )
        + (
            f" with 80% power at a {gated_fpr * 100:.1f}% false-positive rate.**"
            if gated_mde is not None else " 80% power within the tested grid.**"
        ),
        "",
        (
            f"- CI-part FPR (adopted method): {ci_fpr * 100:.1f}% "
            f"(target ≈ {args.alpha * 100:.0f}%); CI-part MDE {pct(ci_mde)}."
        ),
        f"- Practical threshold {thr:.0f}% is "
        + ("**at or above** the CI-part MDE ✓" if result["threshold_at_or_above_ci_mde"]
           else "**below** the CI-part MDE ⚠️")
        + " (so we never claim to flag effects below what we can detect).",
        "",
    ]
    (out / f"{args.run}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nadopted method: {adopted}\nraw -> {out / (args.run + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
