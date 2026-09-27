"""τ²-bench scorer.

τ²'s success criterion (final database state + required outputs) can only be computed with the
τ² package, which is isolated in its own venv behind the agent's domain server. So the **agent**
computes τ²'s reward and reports it in ``RunResult.info``; this scorer simply surfaces it (the
engine never imports tau2). ``reward`` is 0.0/1.0 (DB-state match); ``passed`` is reward == 1.
"""

from __future__ import annotations

from vigil import RunResult

from engine.datasets.base import Case

from .base import ScoreResult


class TauBenchReward:
    """Reads the τ²-bench reward the agent recorded in ``result.info`` (``reward``, ``db_match``,
    ``gold_hash``/``agent_hash``, ``reward_basis``). A missing/empty info dict scores 0 — a run
    that produced no reward is a failure, not a silent pass."""

    name = "TauBenchReward"

    def score(self, case: Case, result: RunResult) -> ScoreResult:
        info = getattr(result, "info", None) or {}
        reward = float(info.get("reward", 0.0))
        return ScoreResult(
            name=self.name,
            passed=reward >= 1.0,
            score=reward,
            detail={
                "db_match": bool(info.get("db_match", reward >= 1.0)),
                "reward_basis": info.get("reward_basis"),
                "gold_hash": info.get("gold_hash"),
                "agent_hash": info.get("agent_hash"),
                "turns": info.get("turns"),
            },
        )
