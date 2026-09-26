"""Deterministic scorers for Week 2 (design doc §6): ExactMatch, ExpectedToolCalls, and
RequiredArguments. Each reads its parameters from ``case.expected``, so scorers are stateless
and shareable. ``scorers_for`` picks which apply to a case.
"""

from __future__ import annotations

import re
import string
from collections import Counter

from vigil import RunResult

from engine.datasets.base import Case

from .base import ScoreResult
from .retrieval import NDCG, AllGoldRetrieved, RetrievalRecall

_WS = re.compile(r"\s+")


def _normalize(value) -> str:
    """Normalized text compare: string-ify, strip, casefold, collapse internal whitespace."""
    return _WS.sub(" ", str(value).strip()).casefold()


def _predicted(result: RunResult):
    """The text a scorer compares: the short ``final_answer`` when the agent set one, else the
    full ``output`` (design: final_answer is separate from the full output)."""
    return result.final_answer if result.final_answer is not None else result.output


class ExactMatch:
    """Normalized compare of the agent's final answer to ``expected['answer']``."""

    name = "ExactMatch"

    def score(self, case: Case, result: RunResult) -> ScoreResult:
        expected = case.expected.get("answer")
        pred = _predicted(result)
        passed = _normalize(pred) == _normalize(expected)
        return ScoreResult(
            name=self.name,
            passed=passed,
            score=1.0 if passed else 0.0,
            detail={"expected": expected, "got": pred},
        )


_ARTICLES = {"a", "an", "the"}
_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def _squad_tokens(text) -> list[str]:
    """SQuAD/HotpotQA normalization: lowercase, drop punctuation, drop the articles a/an/the,
    and split on whitespace."""
    lowered = str(text).lower().translate(_PUNCT_TABLE)
    return [tok for tok in lowered.split() if tok not in _ARTICLES]


def _token_f1(pred: str, gold: str) -> tuple[float, float, float]:
    """Token-level F1 over the normalized token multisets (SQuAD convention). Returns
    (f1, precision, recall). When either side is empty, F1 is 1.0 iff both are empty."""
    p_toks = _squad_tokens(pred)
    g_toks = _squad_tokens(gold)
    if not p_toks or not g_toks:
        same = float(p_toks == g_toks)
        return same, same, same
    common = Counter(p_toks) & Counter(g_toks)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0, 0.0, 0.0
    precision = num_same / len(p_toks)
    recall = num_same / len(g_toks)
    f1 = 2 * precision * recall / (precision + recall)
    return f1, precision, recall


class TokenF1:
    """Token-level F1 with SQuAD/HotpotQA-style normalization, comparing the agent's final
    answer to ``expected['answer']`` (used for HotpotQA). ``score`` is the F1; ``passed`` is
    ``f1 >= expected.get('f1_threshold', 1.0)`` (default: exact token match)."""

    name = "TokenF1"

    def score(self, case: Case, result: RunResult) -> ScoreResult:
        gold = case.expected.get("answer")
        pred = _predicted(result)
        f1, precision, recall = _token_f1(pred, gold)
        threshold = float(case.expected.get("f1_threshold", 1.0))
        return ScoreResult(
            name=self.name,
            passed=f1 >= threshold,
            score=f1,
            detail={
                "expected": gold,
                "got": pred,
                "precision": precision,
                "recall": recall,
                "threshold": threshold,
            },
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


# Map an expected-key to the scorer that consumes it, for inference. TokenF1 also reads
# 'answer' but is deliberately NOT inferred — it would double-score with ExactMatch — so it is
# opt-in via expected['scorers'] (e.g. HotpotQA suites request ["TokenF1"]).
_INFERENCE = (
    ("answer", ExactMatch),
    ("tool_calls", ExpectedToolCalls),
    ("arguments", RequiredArguments),
)
# All selectable-by-name scorers (inferred ones plus explicit-only ones like TokenF1 and the
# retrieval scorers). Retrieval scorers are opt-in via expected['scorers'] — they need
# supporting_titles + the agent's retrievals, so they never apply by key inference.
_BY_NAME = {cls.name: cls for _, cls in _INFERENCE}
for _cls in (TokenF1, RetrievalRecall, NDCG, AllGoldRetrieved):
    _BY_NAME[_cls.name] = _cls


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
