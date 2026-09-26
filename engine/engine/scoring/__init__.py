"""Scorers: deterministic first, judge-ready (design doc §6). The ``Scorer`` protocol is the
seam for the Week-3 LLM-as-judge scorers."""

from .base import CaseScore, Scorer, ScoreResult, score_case
from .deterministic import (
    ExactMatch,
    ExpectedToolCalls,
    RequiredArguments,
    TokenF1,
    scorers_for,
)

__all__ = [
    "CaseScore",
    "ScoreResult",
    "Scorer",
    "score_case",
    "ExactMatch",
    "ExpectedToolCalls",
    "RequiredArguments",
    "TokenF1",
    "scorers_for",
]
