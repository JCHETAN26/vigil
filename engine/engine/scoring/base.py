"""Scorer protocol, result type, and per-case aggregation (design doc §6).

A case's ``expected`` selects which scorers apply and their parameters. ``score_case`` runs
them, and aggregates their ``ScoreResult``s into an overall ``passed``, a composite ``score``
(mean of the per-scorer scores), and a per-scorer ``scores`` mapping persisted as jsonb on
``eval_case_results``.

Which scorers *decide* ``passed`` is suite-configurable: ``expected['pass_scorers']`` names
the deciding scorers (all of them must pass); the rest are **informational** — recorded and
reported, but not gating. HotpotQA, for example, sets ``pass_scorers=['TokenF1']`` (with an
F1 threshold of 0.8), so exact match, retrieval recall, nDCG, and AllGoldRetrieved are
informational. With no ``pass_scorers`` declared, every scorer decides (the original
all-must-pass behavior).
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
    surfaces, not a silent pass.

    ``passed`` is decided by the scorers named in ``expected['pass_scorers']`` (all must pass);
    if that key is absent, every scorer decides. A name in ``pass_scorers`` that never ran is a
    configuration error, raised loudly rather than silently ignored."""
    per: dict = {}
    results: list[ScoreResult] = []

    pass_scorers = case.expected.get("pass_scorers")
    deciding_names = set(pass_scorers) if pass_scorers else None

    for scorer in scorers:
        r = scorer.score(case, result)
        results.append(r)
        is_deciding = deciding_names is None or r.name in deciding_names
        per[r.name] = {
            "passed": r.passed,
            "score": r.score,
            "detail": r.detail,
            "deciding": is_deciding,
        }

    if not results:
        return CaseScore(passed=False, score=0.0, scores={})

    if deciding_names is not None:
        ran = {r.name for r in results}
        missing = deciding_names - ran
        if missing:
            raise KeyError(
                f"pass_scorers names scorer(s) that did not run: {sorted(missing)}; "
                f"ran: {sorted(ran)}"
            )
        deciding = [r for r in results if r.name in deciding_names]
    else:
        deciding = results

    passed = all(r.passed for r in deciding)
    composite = sum(r.score for r in results) / len(results)
    return CaseScore(passed=passed, score=composite, scores=per)
