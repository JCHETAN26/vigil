"""Unit tests for the deterministic scorers and per-case aggregation (design §6)."""

from __future__ import annotations

from vigil import RunResult, ToolCall

from engine.datasets.base import Case
from engine.scoring import (
    ExactMatch,
    ExpectedToolCalls,
    RequiredArguments,
    score_case,
    scorers_for,
)


def _case(expected, case_id="c0"):
    return Case(case_id=case_id, input=None, expected=expected, tags=[])


def _result(output="", tool_calls=None):
    return RunResult(output=output, tool_calls=tool_calls or [], trace_id="t")


def test_exact_match_normalizes():
    s = ExactMatch()
    assert s.score(_case({"answer": "437"}), _result("437")).passed
    # Whitespace + case are normalized.
    assert s.score(_case({"answer": "Hello World"}), _result("  hello   world ")).passed
    r = s.score(_case({"answer": "437"}), _result("438"))
    assert not r.passed and r.score == 0.0


def test_expected_tool_calls_set_and_ordered():
    s = ExpectedToolCalls()
    calls = [ToolCall("calculator"), ToolCall("get_weather")]
    # Set match: order-independent by default.
    assert s.score(
        _case({"tool_calls": ["get_weather", "calculator"]}), _result(tool_calls=calls)
    ).passed
    # Ordered match fails when the order differs.
    r = s.score(
        _case({"tool_calls": ["get_weather", "calculator"], "tool_calls_ordered": True}),
        _result(tool_calls=calls),
    )
    assert not r.passed
    # Ordered match passes with the exact order.
    assert s.score(
        _case({"tool_calls": ["calculator", "get_weather"], "tool_calls_ordered": True}),
        _result(tool_calls=calls),
    ).passed


def test_expected_tool_calls_partial_score():
    s = ExpectedToolCalls()
    r = s.score(
        _case({"tool_calls": ["calculator", "get_weather"]}),
        _result(tool_calls=[ToolCall("calculator")]),
    )
    assert not r.passed
    assert r.score == 0.5  # one of two expected present


def test_required_arguments():
    s = RequiredArguments()
    calls = [ToolCall("calculator", {"expression": "2 + 2", "extra": 1})]
    # Present + equal where a value is specified.
    assert s.score(
        _case({"arguments": {"calculator": {"expression": "2 + 2"}}}), _result(tool_calls=calls)
    ).passed
    # null value requires presence only.
    assert s.score(
        _case({"arguments": {"calculator": {"expression": None}}}), _result(tool_calls=calls)
    ).passed
    # Wrong value fails.
    assert not s.score(
        _case({"arguments": {"calculator": {"expression": "9"}}}), _result(tool_calls=calls)
    ).passed
    # Missing tool fails.
    assert not s.score(
        _case({"arguments": {"get_weather": {"location": None}}}), _result(tool_calls=calls)
    ).passed


def test_scorers_for_inference_and_explicit():
    assert [type(x).__name__ for x in scorers_for({"answer": "x"})] == ["ExactMatch"]
    inferred = [
        type(x).__name__ for x in scorers_for({"answer": "x", "tool_calls": [], "arguments": {}})
    ]
    assert inferred == ["ExactMatch", "ExpectedToolCalls", "RequiredArguments"]
    explicit = [type(x).__name__ for x in scorers_for({"scorers": ["ExpectedToolCalls"]})]
    assert explicit == ["ExpectedToolCalls"]


def test_score_case_aggregates():
    case = _case({"answer": "437", "tool_calls": ["calculator"]})
    result = _result(output="437", tool_calls=[ToolCall("calculator")])
    agg = score_case(case, result, scorers_for(case.expected))
    assert agg.passed
    assert agg.score == 1.0
    assert set(agg.scores) == {"ExactMatch", "ExpectedToolCalls"}

    # One failing scorer makes the case fail; composite is the mean.
    bad = _result(output="wrong", tool_calls=[ToolCall("calculator")])
    agg2 = score_case(case, bad, scorers_for(case.expected))
    assert not agg2.passed
    assert agg2.score == 0.5


def test_score_case_no_scorers_is_not_pass():
    agg = score_case(_case({}), _result(), [])
    assert not agg.passed and agg.score == 0.0 and agg.scores == {}
