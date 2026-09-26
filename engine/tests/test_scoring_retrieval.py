"""Unit tests for the retrieval-quality scorers (recall@k, nDCG@k, all-gold-retrieved) and
the merged-ranking construction they share."""

from __future__ import annotations

import math

import pytest
from vigil import Retrieval, RunResult

from engine.datasets.base import Case
from engine.scoring import (
    NDCG,
    AllGoldRetrieved,
    RetrievalRecall,
    merged_ranking,
    scorers_for,
)


def _case(expected):
    return Case(case_id="c0", input=None, expected=expected, tags=[])


def _result(*calls):
    # each call is a list of doc_ids
    return RunResult(output="", retrievals=[Retrieval(query=f"q{i}", doc_ids=list(c)) for i, c in enumerate(calls)])


def test_merged_ranking_dedup_first_occurrence_order():
    r = _result(["A", "B", "C"], ["B", "D", "A", "E"])
    # First-occurrence order across calls, deduped.
    assert merged_ranking(r) == ["A", "B", "C", "D", "E"]


def test_recall_at_k():
    s = RetrievalRecall()
    gold = {"supporting_titles": ["A", "Z"], "recall_k": 5}
    # A within top-5, Z absent -> recall 0.5.
    r = s.score(_case(gold), _result(["A", "B", "C"], ["D", "E", "F"]))
    assert r.score == 0.5 and not r.passed
    # Both gold within top-5 -> recall 1.0, passes.
    r2 = s.score(_case(gold), _result(["A", "Z", "B"]))
    assert r2.score == 1.0 and r2.passed


def test_recall_respects_k_cutoff():
    s = RetrievalRecall()
    # Z is at rank 6, outside top-5, so recall@5 misses it.
    r = s.score(_case({"supporting_titles": ["A", "Z"], "recall_k": 5}),
                _result(["A", "B", "C", "D", "E", "Z"]))
    assert r.score == 0.5


def test_ndcg_perfect_and_imperfect():
    s = NDCG()
    gold = {"supporting_titles": ["A", "B"], "ndcg_k": 10}
    # Gold at ranks 1 and 2 -> perfect nDCG = 1.0.
    assert s.score(_case(gold), _result(["A", "B", "C"])).score == pytest.approx(1.0)
    # Gold at ranks 1 and 3: DCG = 1/log2(2) + 1/log2(4) = 1 + 0.5; IDCG = 1 + 1/log2(3).
    r = s.score(_case(gold), _result(["A", "X", "B"]))
    dcg = 1.0 + 1.0 / math.log2(4)
    idcg = 1.0 + 1.0 / math.log2(3)
    assert r.score == pytest.approx(dcg / idcg)
    assert not r.passed  # imperfect ranking, default threshold 1.0


def test_all_gold_retrieved_is_uncapped():
    s = AllGoldRetrieved()
    gold = {"supporting_titles": ["A", "Z"]}
    # Z is deep in the ranking (beyond any k), but it WAS retrieved -> both gold present.
    r = s.score(_case(gold), _result(["A", "B", "C", "D", "E", "F"], ["Z"]))
    assert r.passed and r.score == 1.0
    # Z never retrieved -> fails, and reports what's missing.
    r2 = s.score(_case(gold), _result(["A", "B"]))
    assert not r2.passed and r2.detail["missing"] == ["Z"]


def test_retrieval_scorers_are_opt_in_by_name():
    # Not inferred from any expected key; selected explicitly.
    names = [type(x).__name__ for x in scorers_for(
        {"scorers": ["ExactMatch", "TokenF1", "RetrievalRecall", "NDCG", "AllGoldRetrieved"]}
    )]
    assert names == ["ExactMatch", "TokenF1", "RetrievalRecall", "NDCG", "AllGoldRetrieved"]
    # supporting_titles alone does not trigger them.
    assert scorers_for({"supporting_titles": ["A"]}) == []
