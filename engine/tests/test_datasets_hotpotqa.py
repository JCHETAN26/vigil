"""Unit tests for the HotpotQA data-shaping helpers and adapter (no download needed)."""

from __future__ import annotations

import json

import pytest

from engine.datasets import get_adapter
from engine.datasets.hotpotqa import (
    HOTPOTQA_SCORERS,
    HotpotQAAdapter,
    build_corpus,
    select_subset,
    stratified_subset,
    supporting_titles,
    to_case_spec,
)


def _rec(_id, q="?", ans="a", typ="bridge", supporting=None, context=None):
    return {
        "_id": _id,
        "question": q,
        "answer": ans,
        "type": typ,
        "level": "hard",
        "supporting_facts": supporting or [],
        "context": context or [],
    }


def test_supporting_titles_dedup_sorted():
    rec = _rec("x", supporting=[["B", 0], ["A", 1], ["A", 3]])
    assert supporting_titles(rec) == ["A", "B"]


def test_to_case_spec():
    rec = _rec("id1", q="Who?", ans="Curie", typ="comparison",
               supporting=[["Marie Curie", 0], ["Pierre Curie", 1]])
    spec = to_case_spec(rec)
    assert spec == {
        "case_id": "id1", "question": "Who?", "answer": "Curie",
        "supporting_titles": ["Marie Curie", "Pierre Curie"], "type": "comparison", "level": "hard",
    }


def test_select_subset_deterministic_by_id():
    recs = [_rec("c"), _rec("a"), _rec("b")]
    assert [r["_id"] for r in select_subset(recs, 2)] == ["a", "b"]


def test_build_corpus_pools_and_dedups_by_title():
    r1 = _rec("1", context=[["Paris", ["Paris is in France. "]], ["France", ["France is a country."]]])
    r2 = _rec("2", context=[["Paris", ["A DIFFERENT paragraph."]], ["Seine", ["A river."]]])
    corpus = build_corpus([r1, r2])
    ids = [d["doc_id"] for d in corpus]
    assert ids == ["Paris", "France", "Seine"]  # deduped by title, first occurrence wins
    paris = next(d for d in corpus if d["doc_id"] == "Paris")
    assert paris["text"] == "Paris is in France. "  # first occurrence's text kept


def test_stratified_subset_matches_type_mix():
    recs = [_rec(f"b{i:02d}", typ="bridge") for i in range(80)]
    recs += [_rec(f"c{i:02d}", typ="comparison") for i in range(20)]
    picked = stratified_subset(recs, 10)
    assert len(picked) == 10
    types = {r["_id"]: r["type"] for r in recs}
    counts: dict[str, int] = {}
    for i in picked:
        counts[types[i]] = counts.get(types[i], 0) + 1
    # 80/20 mix over 10 -> 8 bridge, 2 comparison.
    assert counts == {"bridge": 8, "comparison": 2}


def _write_cases(tmp_path):
    cases = [
        {"case_id": "q1", "question": "Q1?", "answer": "A1",
         "supporting_titles": ["T1", "T2"], "type": "bridge", "level": "hard"},
        {"case_id": "q2", "question": "Q2?", "answer": "yes",
         "supporting_titles": ["T3", "T4"], "type": "comparison", "level": "medium"},
    ]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"version": "v1", "cases": cases}))
    return path


def test_adapter_loads_cases_with_hotpotqa_scorers(tmp_path):
    path = _write_cases(tmp_path)
    adapter = HotpotQAAdapter(str(path))
    assert adapter.name == "hotpotqa" and adapter.version == "v1"
    assert get_adapter("hotpotqa") is HotpotQAAdapter

    cases = list(adapter.load())
    assert [c.case_id for c in cases] == ["q1", "q2"]
    c1 = cases[0]
    assert c1.input == "Q1?"
    assert c1.expected["answer"] == "A1"
    assert c1.expected["supporting_titles"] == ["T1", "T2"]
    assert c1.expected["scorers"] == HOTPOTQA_SCORERS
    assert c1.expected["ndcg_k"] == 10
    assert c1.tags == ["bridge", "hard"]


def test_adapter_subset_filtering(tmp_path):
    path = _write_cases(tmp_path)
    subset = tmp_path / "subset.txt"
    subset.write_text("q2\n")
    adapter = HotpotQAAdapter.from_config({"path": str(path), "subset_path": str(subset)})
    assert [c.case_id for c in adapter.load()] == ["q2"]

    with pytest.raises(ValueError, match="requires a 'path'"):
        HotpotQAAdapter.from_config({})
