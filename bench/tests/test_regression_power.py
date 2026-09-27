"""Fast, DB-free checks of the regression power harness: on a synthetic per-question model the
detector's false-positive rate sits near α and power rises with effect size to full detection.
The full study on the real HotpotQA baseline is produced by bench/regression_power.py."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import regression_power as rp


def _model(n=100, seed=0):
    # A spread of per-question difficulties, like a real suite (some easy, some hard).
    rng = np.random.default_rng(seed)
    return np.clip(rng.beta(2, 2, n), 0.05, 0.95)


def test_false_positive_rate_near_alpha():
    p = _model()
    mask = np.ones(len(p), dtype=bool)
    fpr = rp.detection_rate(
        p, n_trials=3, delta=0.0, shift_mask=mask, n_sims=200, alpha=0.05, n_boot=800, seed=1
    )
    assert fpr <= 0.12, f"false-positive rate {fpr} too high for α=0.05"


def test_power_increases_with_effect_and_reaches_high():
    p = _model()
    mask = np.ones(len(p), dtype=bool)
    curve = rp.power_curve(
        p, deltas=[0.0, 0.10, 0.25], shift_mask=mask, n_trials=3,
        n_sims=150, alpha=0.05, n_boot=800, seed=2,
    )
    assert curve[0.0] < curve[0.1] < curve[0.25]  # monotone in effect size
    assert curve[0.25] >= 0.8  # a large uniform drop is detected almost always


def test_min_detectable_effect_interpolates():
    curve = {0.0: 0.05, 0.05: 0.5, 0.10: 0.9}
    mde = rp.min_detectable_effect(curve, target=0.8)
    assert 0.05 < mde < 0.10  # between the 50% and 90% grid points
    assert rp.min_detectable_effect({0.0: 0.05, 0.1: 0.2}, target=0.8) is None


def test_shift_only_applies_to_masked_questions():
    p = np.array([0.9, 0.9, 0.9, 0.9])
    mask = np.array([True, True, False, False])
    rng = np.random.default_rng(0)
    run = rp.simulate_pass_run(p, n_trials=1000, rng=rng, shift=0.5, shift_mask=mask)
    means = [np.mean(run[str(i)]) for i in range(4)]
    assert means[0] < 0.6 and means[1] < 0.6  # shifted ~0.4
    assert means[2] > 0.8 and means[3] > 0.8  # unshifted ~0.9
