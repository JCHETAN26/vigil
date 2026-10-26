"""Unit tests for the paired bootstrap regression detector (engine/engine/regression.py)."""

from __future__ import annotations

import numpy as np
import pytest
from triage_fakes import block_scipy

from engine.regression import (
    LOWER_IS_BETTER,
    StatsDependencyError,
    paired_bootstrap_diff,
    stats_backend,
)


def _series(values_per_case):
    """values_per_case: {case_id: [trial values]}."""
    return dict(values_per_case)


def test_no_difference_is_not_flagged():
    # Identical runs → Δ≈0, CI includes zero, not a regression.
    base = {f"q{i}": [1.0, 0.0, 1.0] for i in range(40)}
    v = paired_bootstrap_diff(base, dict(base), metric="TokenF1", threshold=0.03)
    assert not v.regressed
    assert not v.ci_excludes_zero
    assert abs(v.point_estimate) < 1e-9


def test_clear_large_drop_is_flagged():
    # Every question drops by ~0.3 → both decision parts fire.
    base = {f"q{i}": [0.9, 0.9, 0.9] for i in range(40)}
    cand = {f"q{i}": [0.6, 0.6, 0.6] for i in range(40)}
    v = paired_bootstrap_diff(base, cand, metric="TokenF1", threshold=0.03)
    assert v.point_estimate < 0
    assert v.ci_excludes_zero and v.beyond_threshold and v.regressed
    assert v.tail_prob < 0.05


def test_small_but_clear_shift_below_threshold_is_not_regression():
    # A tiny, consistent 1% drop: statistically clear (CI excludes 0) but below the 3% threshold,
    # so it is reported as "watch", not flagged. Both parts are surfaced.
    rng = np.random.default_rng(0)
    base = {f"q{i}": [float(x) for x in rng.uniform(0.8, 0.9, 3)] for i in range(200)}
    cand = {q: [x - 0.01 for x in vals] for q, vals in base.items()}
    v = paired_bootstrap_diff(base, cand, metric="TokenF1", threshold=0.03)
    assert v.ci_excludes_zero  # the 1% drop is real
    assert not v.beyond_threshold  # ...but below the 3% practical threshold
    assert not v.regressed


def test_lower_is_better_cost_rise_is_flagged():
    base = {f"q{i}": [1.0] for i in range(40)}
    cand = {f"q{i}": [1.5] for i in range(40)}
    v = paired_bootstrap_diff(
        base, cand, metric="cost_usd", direction=LOWER_IS_BETTER, threshold=0.1
    )
    assert v.point_estimate > 0 and v.ci_excludes_zero and v.regressed


def test_improvement_is_not_a_regression():
    base = {f"q{i}": [0.6] for i in range(40)}
    cand = {f"q{i}": [0.9] for i in range(40)}
    v = paired_bootstrap_diff(base, cand, metric="TokenF1", threshold=0.03)
    assert v.point_estimate > 0 and not v.regressed and not v.ci_excludes_zero


def test_seeded_determinism():
    base = {f"q{i}": [0.8, 0.7, 0.9] for i in range(30)}
    cand = {f"q{i}": [0.7, 0.6, 0.8] for i in range(30)}
    a = paired_bootstrap_diff(base, cand, seed=7, n_boot=2000)
    b = paired_bootstrap_diff(base, cand, seed=7, n_boot=2000)
    assert (a.point_estimate, a.ci_bound, a.regressed) == (
        b.point_estimate, b.ci_bound, b.regressed)


def test_two_level_runs_and_matches_point_estimate():
    base = {f"q{i}": [1.0, 0.0, 1.0] for i in range(30)}
    cand = {f"q{i}": [1.0, 0.0, 0.0] for i in range(30)}
    q = paired_bootstrap_diff(base, cand, n_boot=2000, seed=1)
    t = paired_bootstrap_diff(base, cand, n_boot=2000, seed=1, two_level=True)
    # Same point estimate (trial means); the two-level CI is at least as wide (adds trial noise).
    assert q.point_estimate == pytest.approx(t.point_estimate)


def test_no_shared_cases_raises():
    with pytest.raises(ValueError):
        paired_bootstrap_diff({"a": [1.0]}, {"b": [1.0]})


def test_all_ci_methods_selectable_and_agree_on_a_clear_regression():
    base = {f"q{i}": [0.9, 0.9, 0.9] for i in range(50)}
    cand = {f"q{i}": [0.5, 0.5, 0.5] for i in range(50)}
    for m in ("t", "percentile", "bca"):
        v = paired_bootstrap_diff(base, cand, threshold=0.03, method=m, n_boot=2000)
        assert v.method == m
        assert v.regressed  # a clear 0.4 drop is caught by every method


def test_unknown_method_raises():
    with pytest.raises(ValueError):
        paired_bootstrap_diff({"a": [1.0, 0.0]}, {"a": [0.0, 0.0]}, method="nope")


# --- the Student-t quantile comes from SciPy, never an approximation ---

# t.ppf(0.95, 19), the 95% one-sided critical value at n=20 questions (tables: 1.729). The
# normal quantile the engine used to fall back to without SciPy is 1.645, a different bound.
_T_95_DF19 = 1.7291328115213682
_Z_95 = 1.6448536269514722


def _twenty_cases():
    rng = np.random.default_rng(3)
    base = {f"q{i}": [float(x)] for i, x in enumerate(rng.uniform(0.4, 0.9, 20))}
    cand = {q: [v[0] - 0.05 + float(rng.normal(0, 0.03))] for q, v in base.items()}
    return base, cand


def test_t_bound_uses_the_student_t_quantile():
    base, cand = _twenty_cases()
    v = paired_bootstrap_diff(base, cand, method="t", n_boot=1000)
    d = np.array([cand[q][0] - base[q][0] for q in base])
    se = float(np.std(d, ddof=1) / np.sqrt(len(d)))
    assert v.ci_bound == pytest.approx(d.mean() + _T_95_DF19 * se, rel=1e-12)
    # The normal approximation's bound differs far beyond the bundle verifier's 1e-9 tolerance.
    assert v.ci_bound != pytest.approx(d.mean() + _Z_95 * se, rel=1e-6)


def test_missing_scipy_refuses_method_t_instead_of_changing_the_bound(monkeypatch):
    base, cand = _twenty_cases()
    block_scipy(monkeypatch)
    with pytest.raises(StatsDependencyError, match="needs SciPy") as exc:
        paired_bootstrap_diff(base, cand, method="t", n_boot=1000)
    assert "make -C engine install" in str(exc.value)
    assert not isinstance(exc.value, ValueError)  # the gate must not treat it as a skipped metric
    assert stats_backend()["scipy"] is None


def test_percentile_and_bca_do_not_need_scipy(monkeypatch):
    base, cand = _twenty_cases()
    with_scipy = {m: paired_bootstrap_diff(base, cand, method=m, n_boot=1000).ci_bound
                  for m in ("percentile", "bca")}
    block_scipy(monkeypatch)
    for m, bound in with_scipy.items():
        assert paired_bootstrap_diff(base, cand, method=m, n_boot=1000).ci_bound == bound, m


def test_stats_backend_records_numpy_and_scipy():
    import scipy

    assert stats_backend() == {"numpy": np.__version__, "scipy": scipy.__version__,
                               "t_quantile": "scipy.stats.t.ppf"}
