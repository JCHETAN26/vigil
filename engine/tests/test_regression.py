"""Unit tests for the paired bootstrap regression detector (engine/engine/regression.py)."""

from __future__ import annotations

import numpy as np
import pytest

from engine.regression import (
    LOWER_IS_BETTER,
    paired_bootstrap_diff,
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
    assert (a.point_estimate, a.ci_bound, a.regressed) == (b.point_estimate, b.ci_bound, b.regressed)


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
