"""Paired bootstrap regression detection (Week 3, design: eval-engine §Week-3 hand-off).

Compares a candidate agent version against a baseline on the SAME suite (paired by case_id).
For a metric, each question's value is the mean over its trials; the statistic is the mean
paired difference Δ = mean_q(candidate_q − baseline_q). A bootstrap over questions gives Δ's
sampling distribution.

Decision rule (as specified): flag a regression when BOTH hold —
  1. the one-sided (1−α) confidence bound excludes zero in the adverse direction, AND
  2. the point estimate is at or beyond the practical threshold in the adverse direction.
Both parts are reported, so a statistically-clear-but-tiny shift (below threshold) and a
large-but-noisy shift (CI includes zero) are each visible and neither is flagged alone.

"Adverse" depends on the metric: for quality metrics (higher is better) a drop (Δ<0) is
adverse; for cost/latency (lower is better) a rise (Δ>0) is adverse.

Primary test is the question-level bootstrap (trial variance folded into each question's mean).
A two-level bootstrap (resample questions, then resample each question's trials) is available
as a diagnostic to expose trial-to-trial noise; it requires a uniform trial count per question.

Pure/numpy only — no DB — so it is unit-testable directly; the CLI and the bench power harness
supply the per-(case, trial) series.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

_N = NormalDist()
# CI methods for the one-sided bound. "percentile" is the plain bootstrap percentile;
# "bca" is bias-corrected-and-accelerated (fixes the percentile method's anti-conservatism at
# small n); "t" is a paired one-sample t interval on the per-question differences.
CI_METHODS = ("percentile", "bca", "t")


def _t_ppf(q: float, df: int) -> float:
    """t-distribution quantile (scipy if present; normal approximation otherwise — negligible
    for the df~100 suites we use)."""
    try:
        from scipy.stats import t

        return float(t.ppf(q, df))
    except Exception:  # noqa: BLE001
        return _N.inv_cdf(q)

HIGHER_IS_BETTER = "higher_is_better"
LOWER_IS_BETTER = "lower_is_better"

# Metric → direction. Quality metrics regress when they fall; cost/latency when they rise.
METRIC_DIRECTION = {
    "passed": HIGHER_IS_BETTER,
    "TokenF1": HIGHER_IS_BETTER,
    "ExactMatch": HIGHER_IS_BETTER,
    "RetrievalRecall@5": HIGHER_IS_BETTER,
    "RetrievalRecall@10": HIGHER_IS_BETTER,
    "NDCG": HIGHER_IS_BETTER,
    "AllGoldRetrieved": HIGHER_IS_BETTER,
    "cost_usd": LOWER_IS_BETTER,
    "latency_ms": LOWER_IS_BETTER,
}


@dataclass
class RegressionVerdict:
    metric: str
    direction: str
    n_questions: int
    point_estimate: float  # mean paired diff (candidate − baseline)
    ci_bound: float  # the one-sided (1−α) confidence bound nearest zero
    ci_excludes_zero: bool  # part 1: CI excludes 0 in the adverse direction
    beyond_threshold: bool  # part 2: |point estimate| ≥ threshold, adverse direction
    threshold: float
    alpha: float
    method: str  # ci method: percentile | bca | t
    tail_prob: float  # bootstrap prob of landing on the non-adverse side of 0 (p-value analog)
    regressed: bool  # part 1 AND part 2

    def summary(self) -> str:
        arrow = "↓" if self.direction == HIGHER_IS_BETTER else "↑"
        verdict = "REGRESSION" if self.regressed else ("watch" if self.ci_excludes_zero else "ok")
        return (
            f"{self.metric:18s} Δ={self.point_estimate:+.4f} {arrow} "
            f"(1−α bound {self.ci_bound:+.4f}; excl0={self.ci_excludes_zero}; "
            f"≥thr={self.beyond_threshold}; p={self.tail_prob:.3f}) → {verdict}"
        )


def _stacked(series: dict[str, list[float]], cases: list[str]) -> list[np.ndarray]:
    return [np.asarray(series[q], dtype=float) for q in cases]


def _one_sided_bound(boot, d, point, n, *, higher_better, alpha, method):
    """One-sided (1−α) confidence bound on the mean paired difference, oriented toward zero:
    the UPPER bound when higher-is-better (regression if < 0), else the LOWER bound (regression
    if > 0). ``method`` ∈ {percentile, bca, t}."""
    if method == "t":
        se = float(np.std(d, ddof=1) / math.sqrt(n)) if n > 1 else 0.0
        tcrit = _t_ppf(1 - alpha, n - 1) if n > 1 else 0.0
        return point + tcrit * se if higher_better else point - tcrit * se
    if method == "percentile":
        return float(np.quantile(boot, 1 - alpha if higher_better else alpha))
    if method == "bca":
        nb = len(boot)
        prop = float(np.mean(boot < point))
        prop = min(max(prop, 1.0 / (2 * nb)), 1 - 1.0 / (2 * nb))  # avoid ±inf z0
        z0 = _N.inv_cdf(prop)
        # Acceleration from the question jackknife of the statistic (mean of d).
        jack = (d.sum() - d) / (n - 1) if n > 1 else d.copy()
        jbar = jack.mean()
        num = float(np.sum((jbar - jack) ** 3))
        den = 6.0 * (float(np.sum((jbar - jack) ** 2)) ** 1.5)
        a = num / den if den != 0 else 0.0
        z = _N.inv_cdf(1 - alpha) if higher_better else _N.inv_cdf(alpha)
        denom = 1 - a * (z0 + z)
        fallback = (1 - alpha) if higher_better else alpha
        adj = _N.cdf(z0 + (z0 + z) / denom) if denom != 0 else fallback
        return float(np.quantile(boot, min(max(adj, 0.0), 1.0)))
    raise ValueError(f"unknown ci method {method!r}")


def paired_bootstrap_diff(
    baseline: dict[str, list[float]],
    candidate: dict[str, list[float]],
    *,
    metric: str = "metric",
    direction: str = HIGHER_IS_BETTER,
    threshold: float = 0.0,
    alpha: float = 0.05,
    n_boot: int = 10000,
    seed: int = 1234,
    two_level: bool = False,
    method: str = "t",
) -> RegressionVerdict:
    """Paired bootstrap of the mean per-question difference between two runs (see module doc).
    ``method`` chooses the one-sided CI: 't' (default; paired t interval — best-calibrated on
    the HotpotQA suite, see bench/regression_power.py), 'percentile', or 'bca' (bias-corrected
    and accelerated; anti-conservative on small/discrete data here)."""
    if method not in CI_METHODS:
        raise ValueError(f"unknown ci method {method!r}; use one of {CI_METHODS}")
    cases = sorted(set(baseline) & set(candidate))
    if not cases:
        raise ValueError("baseline and candidate share no cases (not a paired comparison)")
    b_trials = _stacked(baseline, cases)
    c_trials = _stacked(candidate, cases)
    b_mean = np.array([t.mean() for t in b_trials])
    c_mean = np.array([t.mean() for t in c_trials])
    d = c_mean - b_mean  # per-question paired difference
    point = float(d.mean())
    n = len(cases)
    rng = np.random.default_rng(seed)

    if not two_level:
        idx = rng.integers(0, n, size=(n_boot, n))
        boot = d[idx].mean(axis=1)
    else:
        k = len(b_trials[0])
        if any(len(t) != k for t in b_trials) or any(len(t) != k for t in c_trials):
            raise ValueError("two-level bootstrap requires a uniform trial count per question")
        B = np.vstack(b_trials)  # (n, k)
        C = np.vstack(c_trials)
        q_idx = rng.integers(0, n, size=(n_boot, n))
        tb = rng.integers(0, k, size=(n_boot, n, k))
        tc = rng.integers(0, k, size=(n_boot, n, k))
        bm = np.take_along_axis(B[q_idx], tb, axis=2).mean(axis=2)  # (n_boot, n)
        cm = np.take_along_axis(C[q_idx], tc, axis=2).mean(axis=2)
        boot = (cm - bm).mean(axis=1)

    if direction not in (HIGHER_IS_BETTER, LOWER_IS_BETTER):
        raise ValueError(f"unknown direction {direction!r}")
    higher_better = direction == HIGHER_IS_BETTER
    ci_bound = _one_sided_bound(
        boot, d, point, n, higher_better=higher_better, alpha=alpha, method=method
    )
    if higher_better:
        ci_excludes_zero = ci_bound < 0
        beyond_threshold = point <= -threshold
        tail_prob = float(np.mean(boot >= 0))  # bootstrap evidence the candidate is NOT worse
    else:
        ci_excludes_zero = ci_bound > 0
        beyond_threshold = point >= threshold
        tail_prob = float(np.mean(boot <= 0))

    return RegressionVerdict(
        metric=metric,
        direction=direction,
        n_questions=n,
        point_estimate=point,
        ci_bound=ci_bound,
        ci_excludes_zero=ci_excludes_zero,
        beyond_threshold=beyond_threshold,
        threshold=threshold,
        alpha=alpha,
        method=method,
        tail_prob=tail_prob,
        regressed=ci_excludes_zero and beyond_threshold,
    )
