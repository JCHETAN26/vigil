"""Unit tests for the pass^k estimator (bench/tau2_pass_k.py)."""

from __future__ import annotations

import sys
from math import comb, isclose
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import tau2_pass_k as pk


def test_pass_hat_k_basic():
    # 3 trials, 2 successes.
    assert pk.pass_hat_k(3, 3, 1) == 1.0  # always succeeds
    assert pk.pass_hat_k(3, 0, 1) == 0.0  # never succeeds
    # pass^1 = success rate = c/n.
    assert isclose(pk.pass_hat_k(3, 2, 1), 2 / 3)
    # pass^2 with 2/3 successes = C(2,2)/C(3,2) = 1/3.
    assert isclose(pk.pass_hat_k(3, 2, 2), comb(2, 2) / comb(3, 2))
    assert isclose(pk.pass_hat_k(3, 2, 2), 1 / 3)
    # pass^3 with 2/3 successes = 0 (fewer than 3 successes).
    assert pk.pass_hat_k(3, 2, 3) == 0.0
    # pass^3 with 3/3 = 1.
    assert pk.pass_hat_k(3, 3, 3) == 1.0


def test_pass_hat_k_two_trials():
    # 2 trials (the dev run): pass^1 = c/2, pass^2 = 1 iff both pass.
    assert pk.pass_hat_k(2, 1, 1) == 0.5
    assert pk.pass_hat_k(2, 1, 2) == 0.0
    assert pk.pass_hat_k(2, 2, 2) == 1.0


def test_pass_hat_k_validates():
    with pytest.raises(ValueError):
        pk.pass_hat_k(3, 2, 0)  # k < 1
    with pytest.raises(ValueError):
        pk.pass_hat_k(3, 2, 4)  # k > n
    with pytest.raises(ValueError):
        pk.pass_hat_k(3, 5, 1)  # success > n


def test_pass_k_over_tasks_mean_and_skip():
    # Two tasks at 3 trials: one 3/3, one 2/3.
    tasks = [(3, 3), (3, 2)]
    # pass^1 = mean(1.0, 2/3) = 5/6.
    assert isclose(pk.pass_k_over_tasks(tasks, 1), (1.0 + 2 / 3) / 2)
    # pass^3 = mean(1.0, 0.0) = 0.5.
    assert isclose(pk.pass_k_over_tasks(tasks, 3), 0.5)
    # A task with fewer than k trials is skipped, not counted as 0.
    mixed = [(3, 3), (1, 1)]
    assert pk.pass_k_over_tasks(mixed, 3) == 1.0  # only the 3-trial task qualifies
    assert pk.pass_k_over_tasks([], 1) == 0.0


def test_pass_k_curve():
    tasks = [(3, 3), (3, 2), (3, 1)]
    curve = pk.pass_k_curve(tasks)
    assert set(curve) == {1, 2, 3}
    assert isclose(curve[1], (1.0 + 2 / 3 + 1 / 3) / 3)  # mean success rate
    # pass^3 = mean(1, 0, 0) = 1/3.
    assert isclose(curve[3], 1 / 3)
