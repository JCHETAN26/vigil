"""Scorer protocol, result type, and per-case aggregation (design doc §6).

A case's ``expected`` selects which scorers apply and their parameters. ``score_case`` runs
them, and aggregates their ``ScoreResult``s into an overall ``passed`` (all must pass), a
composite ``score`` (mean of the per-scorer scores), and a per-scorer ``scores`` mapping
persisted as jsonb on ``eval_case_results``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from vigil import RunResult

from engine.datasets.base import Case


@dataclass
class ScoreResult:
    name: str
    passed: bool
    score: float  # 0.0–1.0
    detail: dict = field(default_factory=dict)


@runtime_checkable
class Scorer(Protocol):
    name: str

    def score(self, case: Case, result: RunResult) -> ScoreResult: ...


@dataclass
class CaseScore:
    passed: bool
    score: float
    scores: dict  # {scorer_name -> {passed, score, detail}}


def score_case(case: Case, result: RunResult, scorers: list[Scorer]) -> CaseScore:
    """Run ``scorers`` and aggregate. With no scorers, the case is vacuously not-passed with
    an empty score set — a case with no scoring spec is a configuration error the caller
    surfaces, not a silent pass."""
    per: dict = {}
    results: list[ScoreResult] = []
    for scorer in scorers:
        r = scorer.score(case, result)
        results.append(r)
        per[r.name] = {"passed": r.passed, "score": r.score, "detail": r.detail}

    if not results:
        return CaseScore(passed=False, score=0.0, scores={})

    passed = all(r.passed for r in results)
    composite = sum(r.score for r in results) / len(results)
    return CaseScore(passed=passed, score=composite, scores=per)
