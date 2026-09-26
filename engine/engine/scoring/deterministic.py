"""Deterministic scorers for Week 2 (design doc §6): ExactMatch, ExpectedToolCalls, and
RequiredArguments. Each reads its parameters from ``case.expected``, so scorers are stateless
and shareable. ``scorers_for`` picks which apply to a case.
"""

from __future__ import annotations

import re

from vigil import RunResult

from engine.datasets.base import Case

from .base import ScoreResult

_WS = re.compile(r"\s+")


def _normalize(value) -> str:
    """Normalized text compare: string-ify, strip, casefold, collapse internal whitespace."""
    return _WS.sub(" ", str(value).strip()).casefold()


class ExactMatch:
    """Normalized compare of ``result.output`` to ``expected['answer']``."""

    name = "ExactMatch"

    def score(self, case: Case, result: RunResult) -> ScoreResult:
        expected = case.expected.get("answer")
        got = _normalize(result.output)
        want = _normalize(expected)
        passed = got == want
        return ScoreResult(
            name=self.name,
            passed=passed,
            score=1.0 if passed else 0.0,
            detail={"expected": expected, "got": result.output},
        )


class ExpectedToolCalls:
    """The tool names in ``result.tool_calls`` match ``expected['tool_calls']``. By default a
    set match (order-independent); set ``expected['tool_calls_ordered'] = true`` for an exact
    ordered-sequence match."""

    name = "ExpectedToolCalls"

    def score(self, case: Case, result: RunResult) -> ScoreResult:
        expected = list(case.expected.get("tool_calls", []))
        ordered = bool(case.expected.get("tool_calls_ordered", False))
        actual = [tc.name for tc in result.tool_calls]

        if ordered:
            passed = actual == expected
        else:
            passed = set(actual) == set(expected)

        exp_set = set(expected)
        if exp_set:
            overlap = len(exp_set & set(actual)) / len(exp_set)
        else:
            overlap = 1.0 if not actual else 0.0
        return ScoreResult(
            name=self.name,
            passed=passed,
            score=1.0 if passed else overlap,
            detail={"expected": expected, "actual": actual, "ordered": ordered},
        )


class RequiredArguments:
    """For named tool calls, required arguments are present and (where a value is specified)
    equal. ``expected['arguments'] = {tool_name: {arg: value_or_null}}``; a ``null`` value
    only requires presence."""

    name = "RequiredArguments"

    def score(self, case: Case, result: RunResult) -> ScoreResult:
        specs: dict = case.expected.get("arguments", {})
        by_name: dict[str, list[dict]] = {}
        for tc in result.tool_calls:
            by_name.setdefault(tc.name, []).append(tc.arguments or {})

        satisfied: dict[str, bool] = {}
        for tool_name, required in specs.items():
            calls = by_name.get(tool_name, [])
            satisfied[tool_name] = any(_args_satisfy(args, required) for args in calls)

        total = len(specs)
        ok = sum(1 for v in satisfied.values() if v)
        passed = total > 0 and ok == total
        score = (ok / total) if total else 1.0
        return ScoreResult(
            name=self.name,
            passed=passed,
            score=score,
            detail={"per_tool": satisfied, "required": specs},
        )


def _args_satisfy(args: dict, required: dict) -> bool:
    for key, want in required.items():
        if key not in args:
            return False
        if want is not None and args[key] != want:
            return False
    return True


# Map an expected-key to the scorer that consumes it, for inference.
_INFERENCE = (
    ("answer", ExactMatch),
    ("tool_calls", ExpectedToolCalls),
    ("arguments", RequiredArguments),
)
_BY_NAME = {cls.name: cls for _, cls in _INFERENCE}


def scorers_for(expected: dict) -> list:
    """Select the scorers that apply to a case. If ``expected['scorers']`` names them
    explicitly, use exactly those; otherwise infer from which expected keys are present."""
    explicit = expected.get("scorers")
    if explicit:
        out = []
        for name in explicit:
            if name not in _BY_NAME:
                raise KeyError(f"unknown scorer {name!r}; known: {sorted(_BY_NAME)}")
            out.append(_BY_NAME[name]())
        return out
    return [cls() for key, cls in _INFERENCE if key in expected]
