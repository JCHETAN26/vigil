"""pass^k for repeated eval trials (Yao et al., the τ-bench reliability metric).

pass^k answers "if we sampled k of a task's independent trials, what's the chance *all k*
succeed?" — a stricter, reliability-focused metric than pass@k (which asks if *any* of k
succeed). For a task run n times with c successes, the unbiased estimator averages over all
C(n, k) size-k subsets of its trials:

    pass^k(task) = C(c, k) / C(n, k)      (0 when c < k)

and the reported pass^k is the mean over tasks. pass^1 is the plain success rate. With n
trials we can report k = 1..n (e.g. the 3-trial baseline gives pass^1, pass^2, pass^3).

This module is pure (no DB) so the estimator is unit-tested directly; the τ² baseline script
imports it and feeds per-task success counts pulled from Postgres.
"""

from __future__ import annotations

from math import comb


def pass_hat_k(n_trials: int, n_success: int, k: int) -> float:
    """The single-task pass^k estimator C(c, k) / C(n, k). Requires 1 <= k <= n_trials and
    0 <= n_success <= n_trials; returns 0.0 when the task has fewer than k successes."""
    if k < 1 or k > n_trials:
        raise ValueError(f"k={k} must be in 1..{n_trials}")
    if not 0 <= n_success <= n_trials:
        raise ValueError(f"n_success={n_success} must be in 0..{n_trials}")
    if n_success < k:
        return 0.0
    return comb(n_success, k) / comb(n_trials, k)


def pass_k_over_tasks(successes: list[tuple[int, int]], k: int) -> float:
    """Mean pass^k over tasks. ``successes`` is one ``(n_trials, n_success)`` per task. Tasks
    with fewer than k trials are skipped (pass^k is undefined for them). Returns 0.0 when no
    task qualifies."""
    vals = [pass_hat_k(n, c, k) for (n, c) in successes if n >= k]
    return sum(vals) / len(vals) if vals else 0.0


def pass_k_curve(successes: list[tuple[int, int]]) -> dict[int, float]:
    """pass^k for every k from 1 up to the largest trial count present. Keyed by k."""
    if not successes:
        return {}
    max_k = max(n for (n, _c) in successes)
    return {k: pass_k_over_tasks(successes, k) for k in range(1, max_k + 1)}
