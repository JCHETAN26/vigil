"""Unit tests for the pure aggregation logic in bench/hotpotqa_baseline.py.

These test the statistics (per-question averaging of trials, question-level bootstrap CIs,
separately-reported trial-to-trial variance, per-type breakdown) and the transcript re-scoring,
with no database — the DB layer is exercised by running the script against a real run.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the bench package importable; importing it also puts the engine on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hotpotqa_baseline as hb


def _unit(case_id, typ, metrics, trial=0, cost=0.0):
    return {
        "case_id": case_id,
        "trial": trial,
        "type": typ,
        "metrics": metrics,
        "cost_usd": cost,
        "input_tokens": 100,
        "output_tokens": 20,
        "latency_ms": 500,
    }


def test_per_question_means_averages_trials():
    units = [
        _unit("q1", "bridge", {"TokenF1": 1.0, "passed": 1.0}, trial=0),
        _unit("q1", "bridge", {"TokenF1": 0.0, "passed": 0.0}, trial=1),
        _unit("q2", "comparison", {"TokenF1": 0.5, "passed": 0.0}, trial=0),
    ]
    qm = hb.per_question_means(units)
    assert qm["q1"]["means"]["TokenF1"] == 0.5  # (1.0 + 0.0) / 2
    assert qm["q1"]["means"]["passed"] == 0.5
    assert qm["q1"]["n_trials"] == 2
    assert qm["q2"]["means"]["TokenF1"] == 0.5
    assert qm["q2"]["type"] == "comparison"


def test_bootstrap_ci_is_deterministic_and_brackets_mean():
    values = [0.0, 0.5, 1.0, 0.75, 0.25]
    a = hb.bootstrap_ci(values, n_boot=2000, seed=7)
    b = hb.bootstrap_ci(values, n_boot=2000, seed=7)
    assert a == b  # same seed -> identical CI
    assert a["mean"] == sum(values) / len(values)
    assert a["lo"] <= a["mean"] <= a["hi"]
    assert a["n"] == 5


def test_bootstrap_ci_degenerate_cases():
    assert hb.bootstrap_ci([], n_boot=10)["mean"] is None
    one = hb.bootstrap_ci([0.42], n_boot=10)
    assert one["mean"] == one["lo"] == one["hi"] == 0.42
    # A constant sample has a zero-width interval.
    const = hb.bootstrap_ci([0.3, 0.3, 0.3], n_boot=500, seed=1)
    assert const["lo"] == const["hi"] == 0.3


def test_trial_variance_reported_separately():
    # q1 varies across its 2 trials (std of {1,0} = 0.5); q2 has a single trial (ignored).
    units = [
        _unit("q1", "bridge", {"TokenF1": 1.0}, trial=0),
        _unit("q1", "bridge", {"TokenF1": 0.0}, trial=1),
        _unit("q2", "bridge", {"TokenF1": 0.8}, trial=0),
    ]
    tv = hb.trial_variance(units, "TokenF1")
    assert tv["n_questions"] == 1  # only q1 has >= 2 trials
    assert tv["mean_std"] == 0.5
    assert tv["max_std"] == 0.5


def test_trial_variance_none_for_single_trial_run():
    units = [_unit("q1", "bridge", {"TokenF1": 1.0}), _unit("q2", "bridge", {"TokenF1": 0.5})]
    tv = hb.trial_variance(units, "TokenF1")
    assert tv["mean_std"] is None and tv["n_questions"] == 0


def test_aggregate_overall_and_by_type_and_cost():
    units = [
        _unit("q1", "bridge", {"TokenF1": 1.0, "passed": 1.0}, cost=0.01),
        _unit("q2", "bridge", {"TokenF1": 0.0, "passed": 0.0}, cost=0.02),
        _unit("q3", "comparison", {"TokenF1": 0.5, "passed": 0.0}, cost=0.03),
    ]
    agg = hb.aggregate(units, ["passed", "TokenF1"], n_boot=500, seed=1)
    # Overall TokenF1 mean = mean of per-question means = (1.0 + 0.0 + 0.5)/3.
    assert abs(agg["overall"]["metrics"]["TokenF1"]["ci"]["mean"] - 0.5) < 1e-9
    assert agg["overall"]["n_questions"] == 3
    # Per-type splits the questions.
    assert agg["by_type"]["bridge"]["n_questions"] == 2
    assert agg["by_type"]["comparison"]["n_questions"] == 1
    assert abs(agg["by_type"]["bridge"]["metrics"]["TokenF1"]["ci"]["mean"] - 0.5) < 1e-9
    # Cost totals and per-question.
    assert abs(agg["cost"]["total_usd"] - 0.06) < 1e-9
    assert abs(agg["cost"]["per_question_usd"] - 0.02) < 1e-9


def test_rescore_unit_uses_fixed_scorers():
    # "The Yoruba" gold vs "Yoruba" answer: fixed ExactMatch (SQuAD normalization) -> 1.0,
    # TokenF1 -> 1.0, and pass_scorers=['TokenF1'] with f1_threshold 0.8 -> passed.
    expected = {
        "answer": "The Yoruba",
        "supporting_titles": ["Yoruba people", "Ida (sword)"],
        "type": "bridge",
        "ndcg_k": 10,
        "scorers": [
            "ExactMatch",
            "TokenF1",
            "RetrievalRecall@5",
            "RetrievalRecall@10",
            "NDCG",
            "AllGoldRetrieved",
        ],
        "pass_scorers": ["TokenF1"],
        "f1_threshold": 0.8,
    }
    output = {
        "output": "... FINAL: Yoruba",
        "final_answer": "Yoruba",
        "tool_calls": [{"name": "search", "arguments": {"query": "x"}}],
        "retrievals": [{"query": "x", "doc_ids": ["Yoruba people", "Ida (sword)", "Other"]}],
    }
    row = hb.rescore_unit(expected, ["bridge"], output)
    assert row["ExactMatch"] == 1.0  # would have been 0.0 before the normalization fix
    assert row["TokenF1"] == 1.0
    assert row["passed"] == 1.0
    assert row["AllGoldRetrieved"] == 1.0
    assert row["RetrievalRecall@5"] == 1.0


def test_runresult_from_output_roundtrip():
    output = {
        "output": "full",
        "final_answer": "ans",
        "tool_calls": [{"name": "search", "arguments": {"query": "q"}}],
        "retrievals": [{"query": "q", "doc_ids": ["a", "b"]}],
    }
    rr = hb.runresult_from_output(output)
    assert rr.final_answer == "ans"
    assert rr.tool_calls[0].name == "search"
    assert rr.retrievals[0].doc_ids == ["a", "b"]
